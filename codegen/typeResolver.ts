/**
 * TypeScript Compiler API helpers for resolving gitbeaker method signatures.
 *
 * Walks the @gitbeaker/core .d.ts to extract:
 *   - positional arg types (e.g. `title: string` → `str`)
 *   - options object properties (name, optional/nullable, mapped Python type)
 *   - whether options has an index signature `[k: string]: any` (→ keep **options)
 *
 * Falls back gracefully: when a type can't be mapped or a signature is too
 * exotic, returns `resolved: false` and the caller emits the legacy
 * **options-only shape.
 */
import * as ts from "typescript";

// ── Public types ───────────────────────────────────────────────────────────

export interface PropertySpec {
  name: string;       // wire/snake name (e.g. "source_branch")
  pyName: string;     // python identifier (e.g. "source_branch", suffixed with _ for keywords)
  pyType: string;     // mapped Python type WITHOUT optional/nullable decoration
  optional: boolean;  // TS `foo?:`
  nullable: boolean;  // TS includes `null` in the type
}

export interface PositionalArgSpec {
  name: string;       // TS arg name (e.g. "sourceBranch")
  pyName: string;     // python identifier (snake_case of name)
  pyType: string;     // mapped Python type
}

export interface OptionsTypeInfo {
  properties: PropertySpec[];
  hasIndexSignature: boolean; // → emit **options on the generated fn
  resolved: boolean;          // false = caller falls back to legacy shape
}

export interface MethodTypeInfo {
  positionalArgs: PositionalArgSpec[];
  options: OptionsTypeInfo;
}

// ── Internals ──────────────────────────────────────────────────────────────

const PY_KEYWORDS = new Set([
  "False", "None", "True", "and", "as", "assert", "async", "await",
  "break", "class", "continue", "def", "del", "elif", "else", "except",
  "finally", "for", "from", "global", "if", "import", "in", "is",
  "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try",
  "while", "with", "yield",
]);

// Gitbeaker-internal props that don't translate to GitLab REST: showExpanded
// wraps the response, asAdmin/asStream/isForm switch client behavior, none
// of which our Python wrapper exposes. `sudo` IS a real GitLab API param
// (sent as a query field / Sudo header) so we keep it as a typed param.
const MIDDLEWARE_PROPS = new Set([
  "showExpanded", "asAdmin", "asStream", "isForm",
]);

function toSnake(s: string): string {
  return s
    .replace(/([A-Z]{2,})([A-Z][a-z])/g, "$1_$2")
    .replace(/([a-z\d])([A-Z])/g, "$1_$2")
    .toLowerCase();
}

function pyName(snake: string): string {
  return PY_KEYWORDS.has(snake) ? snake + "_" : snake;
}

export function loadChecker(typesEntry: string) {
  const program = ts.createProgram({
    rootNames: [typesEntry],
    options: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext,
      moduleResolution: ts.ModuleResolutionKind.NodeNext,
      strict: false,
      skipLibCheck: true,
      noEmit: true,
    },
  });
  const checker = program.getTypeChecker();
  const source = program.getSourceFile(typesEntry);
  if (!source) throw new Error(`Could not load ${typesEntry}`);
  return { program, checker, source };
}

function findClass(source: ts.SourceFile, className: string): ts.ClassDeclaration | null {
  for (const stmt of source.statements) {
    if (ts.isClassDeclaration(stmt) && stmt.name?.text === className) {
      return stmt;
    }
  }
  return null;
}

function findOverloads(
  classDecl: ts.ClassDeclaration,
  methodName: string,
): ts.MethodDeclaration[] {
  const out: ts.MethodDeclaration[] = [];
  for (const member of classDecl.members) {
    if (
      ts.isMethodDeclaration(member) &&
      ts.isIdentifier(member.name) &&
      member.name.text === methodName
    ) {
      out.push(member);
    }
  }
  return out;
}

interface OverloadInfo {
  decl: ts.MethodDeclaration;
  positionalSymbols: ts.Symbol[];
  positionalTypes: ts.Type[];
  optionsSymbol: ts.Symbol | null;
  optionsType: ts.Type | null;
  optionsOptional: boolean;
}

function inspectOverload(
  decl: ts.MethodDeclaration,
  checker: ts.TypeChecker,
): OverloadInfo | null {
  const sig = checker.getSignatureFromDeclaration(decl);
  if (!sig) return null;
  const params = sig.getParameters();
  let optionsSymbol: ts.Symbol | null = null;
  let positionalSymbols: ts.Symbol[] = params;
  if (params.length > 0) {
    const last = params[params.length - 1];
    const lastName = last.getName();
    if (lastName === "options" || lastName.startsWith("__")) {
      optionsSymbol = last;
      positionalSymbols = params.slice(0, -1);
    }
  }
  const positionalTypes = positionalSymbols.map((s) => {
    try {
      return checker.getTypeOfSymbolAtLocation(s, decl);
    } catch {
      return checker.getAnyType();
    }
  });
  let optionsType: ts.Type | null = null;
  let optionsOptional = true;
  if (optionsSymbol) {
    try {
      optionsType = checker.getTypeOfSymbolAtLocation(optionsSymbol, decl);
    } catch {
      optionsType = null;
    }
    optionsOptional = (optionsSymbol.flags & ts.SymbolFlags.Optional) !== 0;
  }
  return {
    decl,
    positionalSymbols,
    positionalTypes,
    optionsSymbol,
    optionsType,
    optionsOptional,
  };
}

export function resolveMethod(
  checker: ts.TypeChecker,
  source: ts.SourceFile,
  klass: string,
  method: string,
): MethodTypeInfo | null {
  const classDecl = findClass(source, klass);
  if (!classDecl) return null;
  const decls = findOverloads(classDecl, method);
  if (decls.length === 0) return null;

  const overloads: OverloadInfo[] = [];
  for (const d of decls) {
    const info = inspectOverload(d, checker);
    if (info) overloads.push(info);
  }
  if (overloads.length === 0) return null;

  // Use the LAST overload's positional NAMES (gitbeaker keeps names consistent
  // across overloads), but MERGE types across overloads at each index — this
  // recovers cases like `Search.all(scope: 'users'|'notes'|... overloads)` that
  // would otherwise collapse to a single literal.
  const last = overloads[overloads.length - 1];
  const positionalArgs: PositionalArgSpec[] = [];
  for (let i = 0; i < last.positionalSymbols.length; i++) {
    const sym = last.positionalSymbols[i];
    const typesAtIdx: ts.Type[] = [];
    for (const ov of overloads) {
      if (ov.positionalTypes[i]) typesAtIdx.push(ov.positionalTypes[i]);
    }
    const pyType = mapTypesUnion(typesAtIdx, checker);
    const name = sym.getName();
    const snake = toSnake(name);
    positionalArgs.push({ name, pyName: pyName(snake), pyType });
  }

  // Merge options across overloads. A property is OPTIONAL if any of these hold:
  //   - the options parameter itself is `options?:` in any overload,
  //   - the property is declared `?:` in any overload,
  //   - the property is missing from at least one overload's options shape.
  const optionsTypes = overloads
    .map((o) => o.optionsType)
    .filter((t): t is ts.Type => t !== null);
  let options: OptionsTypeInfo = {
    properties: [],
    hasIndexSignature: false,
    resolved: false,
  };
  if (optionsTypes.length > 0) {
    const anyOptionsOptional = overloads.some((o) => o.optionsOptional);
    options = mergeOptionsTypes(
      optionsTypes,
      checker,
      last.decl,
      anyOptionsOptional,
    );
  }

  return { positionalArgs, options };
}

function mergeOptionsTypes(
  types: ts.Type[],
  checker: ts.TypeChecker,
  decl: ts.Node,
  anyOptionsOptional: boolean,
): OptionsTypeInfo {
  const totalOverloads = types.length;
  type PropInfo = {
    typesAcrossOverloads: ts.Type[];
    presenceCount: number;
    optionalCount: number; // declared `?:` or `markOptional` (union-absent)
    nullable: boolean;
    name: string;
    pyName: string;
  };
  const props = new Map<string, PropInfo>();
  let hasIndexSignature = false;

  for (const t of types) {
    if (hasUnionIndexSignature(t, checker)) hasIndexSignature = true;
    for (const { sym, markOptional } of collectProperties(t, checker)) {
      const propName = sym.getName();
      if (MIDDLEWARE_PROPS.has(propName)) continue;
      const declaredOpt = (sym.flags & ts.SymbolFlags.Optional) !== 0;
      const propDecl = sym.valueDeclaration ?? sym.declarations?.[0] ?? decl;
      let propType: ts.Type;
      try {
        propType = checker.getTypeOfSymbolAtLocation(sym, propDecl);
      } catch {
        propType = checker.getAnyType();
      }
      const nullable = isNullableType(propType);
      const snake = toSnake(propName);
      const existing = props.get(propName);
      if (existing) {
        existing.typesAcrossOverloads.push(propType);
        existing.presenceCount++;
        if (declaredOpt || markOptional) existing.optionalCount++;
        if (nullable) existing.nullable = true;
      } else {
        props.set(propName, {
          typesAcrossOverloads: [propType],
          presenceCount: 1,
          optionalCount: declaredOpt || markOptional ? 1 : 0,
          nullable,
          name: snake,
          pyName: pyName(snake),
        });
      }
    }
  }

  const properties: PropertySpec[] = [];
  for (const info of props.values()) {
    const optional =
      anyOptionsOptional ||
      info.optionalCount > 0 ||
      info.presenceCount < totalOverloads;
    const pyType = mapTypesUnion(info.typesAcrossOverloads, checker);
    properties.push({
      name: info.name,
      pyName: info.pyName,
      pyType,
      optional,
      nullable: info.nullable,
    });
  }

  return { properties, hasIndexSignature, resolved: true };
}

function mapTypesUnion(types: ts.Type[], checker: ts.TypeChecker): string {
  if (types.length === 0) return "Any";
  if (types.length === 1) return mapType(types[0], checker);

  // Flatten unions: walk each input type and collect every member.
  const flat: ts.Type[] = [];
  for (const t of types) {
    const s = stripUndefined(t);
    if (s.isUnion()) {
      for (const m of s.types) flat.push(m);
    } else {
      flat.push(s);
    }
  }

  const allString = flat.every((t) => t.isStringLiteral());
  if (allString && flat.length > 0) {
    const vals = [
      ...new Set(flat.map((t) => (t as ts.StringLiteralType).value)),
    ];
    return `Literal[${vals.map((v) => `"${v}"`).join(", ")}]`;
  }
  const allNumber = flat.every((t) => t.isNumberLiteral());
  if (allNumber && flat.length > 0) {
    const vals = [
      ...new Set(flat.map((t) => (t as ts.NumberLiteralType).value)),
    ];
    return `Literal[${vals.join(", ")}]`;
  }
  const parts = types.map((t) => mapType(t, checker));
  return [...new Set(parts)].join(" | ");
}

function stripUndefined(type: ts.Type): ts.Type {
  if (type.isUnion()) {
    const non = type.types.filter(
      (t) => (t.flags & ts.TypeFlags.Undefined) === 0,
    );
    if (non.length === 1) return non[0];
  }
  return type;
}

function isNullableType(type: ts.Type): boolean {
  if (type.isUnion()) {
    return type.types.some((t) => (t.flags & ts.TypeFlags.Null) !== 0);
  }
  return (type.flags & ts.TypeFlags.Null) !== 0;
}

/**
 * Collect all properties accessible from `type`, accounting for unions.
 *
 * For unions: `type.getProperties()` returns only props common to ALL
 * members. Combinator types like `SomeOf<{a,b}> = ({a} | {b})` and
 * `OneOrNoneOf<>` strip away the per-member props this way. We walk each
 * union member individually and aggregate, marking any prop not present in
 * every member as optional.
 *
 * When the same prop appears in multiple union members with different
 * types (e.g. `OneOf<{projectId, groupId}>` expands to members where in
 * one branch projectId is `string | number` and in another it's `never`),
 * prefer the symbol whose resolved type is NOT `never`.
 */
function symbolTypeFlags(
  prop: ts.Symbol,
  checker: ts.TypeChecker,
): ts.TypeFlags {
  const decl = prop.valueDeclaration ?? prop.declarations?.[0];
  if (!decl) return 0 as ts.TypeFlags;
  try {
    return checker.getTypeOfSymbolAtLocation(prop, decl).flags;
  } catch {
    return 0 as ts.TypeFlags;
  }
}

function collectProperties(
  type: ts.Type,
  checker: ts.TypeChecker,
): { sym: ts.Symbol; markOptional: boolean }[] {
  const apparent = checker.getApparentType(stripUndefined(type));
  if (apparent.isUnion()) {
    // For each name across union members:
    //   - Prefer a symbol whose type isn't `never` (combinator types like
    //     OneOrNoneOf encode "absent" as `never` in a member).
    //   - Mark the prop as optional unless it's required-and-non-never in
    //     EVERY member (i.e., truly required in the combined type).
    const total = apparent.types.length;
    const requiredCount = new Map<string, number>();
    const symByName = new Map<string, ts.Symbol>();
    for (const member of apparent.types) {
      const m = checker.getApparentType(stripUndefined(member));
      for (const prop of m.getProperties()) {
        const name = prop.getName();
        const declaredOpt = (prop.flags & ts.SymbolFlags.Optional) !== 0;
        const propFlags = symbolTypeFlags(prop, checker);
        const isNever = (propFlags & ts.TypeFlags.Never) !== 0;

        // Prefer non-never typed symbol.
        const existing = symByName.get(name);
        if (!existing) {
          symByName.set(name, prop);
        } else if (!isNever) {
          const existingFlags = symbolTypeFlags(existing, checker);
          if ((existingFlags & ts.TypeFlags.Never) !== 0) {
            symByName.set(name, prop);
          }
        }

        if (!declaredOpt && !isNever) {
          requiredCount.set(name, (requiredCount.get(name) ?? 0) + 1);
        }
      }
    }
    const result: { sym: ts.Symbol; markOptional: boolean }[] = [];
    for (const [name, sym] of symByName) {
      const requiredEverywhere = (requiredCount.get(name) ?? 0) === total;
      result.push({ sym, markOptional: !requiredEverywhere });
    }
    return result;
  }
  return apparent
    .getProperties()
    .map((sym) => ({ sym, markOptional: false }));
}

function hasUnionIndexSignature(
  type: ts.Type,
  checker: ts.TypeChecker,
): boolean {
  const apparent = checker.getApparentType(stripUndefined(type));
  if (apparent.isUnion()) {
    return apparent.types.some((m) => {
      const infos = checker.getIndexInfosOfType(
        checker.getApparentType(stripUndefined(m)),
      );
      return infos.some((i) => (i.keyType.flags & ts.TypeFlags.String) !== 0);
    });
  }
  const infos = checker.getIndexInfosOfType(apparent);
  return infos.some((i) => (i.keyType.flags & ts.TypeFlags.String) !== 0);
}

function mapType(type: ts.Type, checker: ts.TypeChecker): string {
  const stripped = stripUndefined(type);

  if (stripped.flags & ts.TypeFlags.String) return "str";
  if (stripped.flags & ts.TypeFlags.Number) return "int";
  if (stripped.flags & ts.TypeFlags.Boolean) return "bool";
  if (stripped.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown)) return "Any";
  if (stripped.flags & ts.TypeFlags.Null) return "None";

  if (stripped.isStringLiteral()) {
    return `Literal["${(stripped as ts.StringLiteralType).value}"]`;
  }
  if (stripped.isNumberLiteral()) {
    return `Literal[${(stripped as ts.NumberLiteralType).value}]`;
  }
  // boolean literal (true/false) → collapse to bool
  if (stripped.flags & ts.TypeFlags.BooleanLiteral) return "bool";

  if (stripped.isUnion()) {
    return mapUnion(stripped, checker);
  }

  const symbol = stripped.getSymbol();
  const symName = symbol?.getName();

  // Array<T> / T[]
  if (symName === "Array") {
    const args = (stripped as ts.TypeReference).typeArguments;
    if (args && args.length === 1) {
      const inner = mapType(args[0], checker);
      return `list[${inner}]`;
    }
    return "list";
  }
  // Tuple types (rare in gitbeaker options)
  if (checker.isTupleType?.(stripped)) {
    return "list";
  }
  // Date → ISO string
  if (symName === "Date") return "str";
  // Blob (file uploads) — caller handles separately, opaque here
  if (symName === "Blob" || symName === "File") return "Any";

  // Generic record / object / index-signature-only type → dict
  if (stripped.flags & ts.TypeFlags.Object) return "dict";

  return "Any";
}

function mapUnion(type: ts.UnionType, checker: ts.TypeChecker): string {
  // Strip undefined/null members; null is tracked separately by isNullableType.
  const members = type.types.filter(
    (t) => (t.flags & (ts.TypeFlags.Undefined | ts.TypeFlags.Null)) === 0,
  );
  if (members.length === 0) return "Any";

  // All-string-literal or all-number-literal → Literal[…]
  const allStringLits = members.every((t) => t.isStringLiteral());
  if (allStringLits) {
    const vals = members.map(
      (t) => `"${(t as ts.StringLiteralType).value}"`,
    );
    return `Literal[${vals.join(", ")}]`;
  }
  const allNumberLits = members.every((t) => t.isNumberLiteral());
  if (allNumberLits) {
    const vals = members.map((t) => `${(t as ts.NumberLiteralType).value}`);
    return `Literal[${vals.join(", ")}]`;
  }

  // Mixed union: join distinct mapped parts with " | ". null is added back
  // by the caller via PropertySpec.nullable, so we don't emit it here.
  const parts = members.map((t) => mapType(t, checker));
  const unique = [...new Set(parts)];
  return unique.join(" | ");
}
