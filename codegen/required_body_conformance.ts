/**
 * Verify that every required request-body property in the vendored OpenAPI
 * spec remains non-omittable and non-nullable in the committed generated
 * wrapper. This parses the raw specification independently of codegen's
 * OpenAPI resolver so resolver regressions cannot redefine the oracle.
 */
import { readFileSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";
import { parse as yamlParse } from "yaml";

import {
  BODY_FIELD_OVERRIDES,
  CONCRETE_DEFAULT_OVERRIDES,
  CONDITIONAL_BRANCH_FIELD_JUDGMENTS,
  DOCUMENTED_SPEC_GAPS,
  PUBLIC_UPLOAD_OVERRIDE_PROOFS,
  GITBEAKER_SOURCE_WIRE_NAME_JUDGMENTS,
  bodyFieldJudgmentKey,
  bodyFieldOverride,
  concreteDefaultOverride,
  documentedSpecGap,
  sourceWireNameJudgmentKey,
  type ConditionalBranchFieldJudgment,
  type ConcreteDefaultOverride,
  type GitbeakerSourceWireNameJudgment,
  type PublicUploadOverrideProof,
} from "./requiredBodyJudgments.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OPENAPI_PATH = join(__dirname, "openapi/openapi_v3.yaml");
const GENERATED_PATH = join(__dirname, "../src/gitlab_mcp/_generated.py");
const TOOLS_PATH = join(__dirname, "../src/gitlab_mcp/tools.py");
const GITBEAKER_IMPLEMENTATION_PATH = join(
  __dirname,
  "node_modules/@gitbeaker/core/dist/index.js",
);
const GITBEAKER_TYPES_PATH = join(
  __dirname,
  "node_modules/@gitbeaker/core/dist/index.d.ts",
);
const PY_KEYWORDS: Record<string, true> = {
  False: true, None: true, True: true, and: true, as: true, assert: true,
  async: true, await: true, break: true, class: true, continue: true,
  def: true, del: true, elif: true, else: true, except: true, finally: true,
  for: true, from: true, global: true, if: true, import: true, in: true,
  is: true, lambda: true, nonlocal: true, not: true, or: true, pass: true,
  raise: true, return: true, try: true, while: true, with: true, yield: true,
};

type JsonObject = Record<string, unknown>;

interface RawBodyField {
  name: string;
  required: boolean;
}

interface RawOpenApiOperation {
  rawPath: string;
  bodyFields: RawBodyField[];
}

interface GeneratedOperation {
  functionName: string;
  operation: string;
  verb: string;
  paths: string[];
  signature: string;
  body: string;
}

interface GeneratedAlias {
  operation: string;
  target: string;
}

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function conformancePath(pathTemplate: string): string {
  let path = pathTemplate
    .replace(/\$\{[^}]+\}/g, "{*}")
    .replace(/\{[^}]+\}/g, "{*}")
    .replace(/^\/api\/v4\//, "/");
  if (!path.startsWith("/")) path = "/" + path;
  if (path.length > 1 && path.endsWith("/")) path = path.slice(0, -1);
  return path;
}

function conformanceKey(verb: string, pathTemplate: string): string {
  const normalizedVerb = (verb === "del" ? "delete" : verb).toUpperCase();
  return `${normalizedVerb} ${conformancePath(pathTemplate)}`;
}

function pythonName(wireName: string): string {
  let name = wireName.replace(/[^A-Za-z0-9_]+/g, "_");
  name = name.replace(/^_+/, "").replace(/_+$/, "");
  if (!name) name = "_";
  return PY_KEYWORDS[name] ? name + "_" : name;
}

function toSnake(name: string): string {
  return name
    .replace(/([A-Z]{2,})([A-Z][a-z])/g, "$1_$2")
    .replace(/([a-z\d])([A-Z])/g, "$1_$2")
    .toLowerCase();
}

function rawBodyFields(
  schema: unknown,
  components: JsonObject,
  depth = 0,
): RawBodyField[] {
  if (!isObject(schema) || depth > 4) return [];

  const reference = schema.$ref;
  if (typeof reference === "string") {
    const match = /^#\/components\/schemas\/(.+)$/.exec(reference);
    const target = match ? components[match[1]] : undefined;
    return target ? rawBodyFields(target, components, depth + 1) : [];
  }

  const requiredNames = new Set(
    Array.isArray(schema.required)
      ? schema.required.filter((name): name is string => typeof name === "string")
      : [],
  );
  const fields = new Map<string, RawBodyField>();
  if (isObject(schema.properties)) {
    for (const name of Object.keys(schema.properties)) {
      fields.set(name, { name, required: requiredNames.has(name) });
    }
  }
  if (Array.isArray(schema.allOf)) {
    for (const part of schema.allOf) {
      for (const field of rawBodyFields(part, components, depth + 1)) {
        const existing = fields.get(field.name);
        fields.set(field.name, {
          name: field.name,
          required: existing?.required === true || field.required,
        });
      }
    }
  }
  return [...fields.values()];
}

function rawOpenApiOperations(source: string): Map<string, RawOpenApiOperation> {
  const spec = yamlParse(source);
  if (!isObject(spec) || !isObject(spec.paths)) {
    throw new Error("Vendored OpenAPI document has no paths object");
  }
  const components = isObject(spec.components) && isObject(spec.components.schemas)
    ? spec.components.schemas
    : {};
  const operations = new Map<string, RawOpenApiOperation>();

  for (const [rawPath, pathItem] of Object.entries(spec.paths)) {
    if (!isObject(pathItem)) continue;
    for (const verb of ["get", "post", "put", "patch", "delete"]) {
      const operation = pathItem[verb];
      if (!isObject(operation)) continue;

      let bodyFields: RawBodyField[] = [];
      if (isObject(operation.requestBody) && isObject(operation.requestBody.content)) {
        const content = operation.requestBody.content;
        const media = content["application/json"]
          ?? content["multipart/form-data"]
          ?? Object.values(content)[0];
        if (isObject(media) && media.schema !== undefined) {
          bodyFields = rawBodyFields(media.schema, components);
        }
      }

      const key = conformanceKey(verb, rawPath);
      if (operations.has(key)) {
        throw new Error(`Normalized OpenAPI path collision in conformance gate: ${key}`);
      }
      operations.set(key, { rawPath, bodyFields });
    }
  }
  return operations;
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function parameterDeclaration(signature: string, name: string): string | null {
  const start = new RegExp(`(?:^\\s*|,\\s*)${escapeRegex(name)}\\s*:`).exec(signature);
  if (!start || start.index === undefined) return null;

  const nameStart = signature.indexOf(name, start.index);
  const remaining = signature.slice(nameStart + name.length);
  const nextParam =
    /,\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*:|\*{1,2}[A-Za-z_][A-Za-z0-9_]*)/.exec(remaining);
  const end = nextParam?.index === undefined
    ? signature.length
    : nameStart + name.length + nextParam.index;
  return signature.slice(nameStart, end).trim().replace(/,$/, "");
}

function generatedFunctions(source: string): { operations: GeneratedOperation[]; aliases: GeneratedAlias[] } {
  const operations: GeneratedOperation[] = [];
  const aliases: GeneratedAlias[] = [];

  for (const chunk of source.split(/^def /m).slice(1)) {
    const newline = chunk.indexOf("\n");
    if (newline === -1) continue;
    const header = chunk.slice(0, newline);
    const headerMatch = /^(\w+)\((.*)\):$/.exec(header);
    if (!headerMatch) continue;

    const [, functionName, signature] = headerMatch;
    const body = chunk.slice(newline + 1);
    const direct = /^    \"\"\"(\w+)\.(\w+) \((GET|POST|PUT|PATCH|DELETE) ([^)]*)\)\./m.exec(body);
    if (direct) {
      const [, klass, method, verb, path] = direct;
      operations.push({
        functionName,
        operation: `${klass}.${method}`,
        verb,
        paths: [path],
        signature,
        body,
      });
      continue;
    }

    const conditional = /^    \"\"\"(\w+)\.(\w+) \((GET|POST|PUT|PATCH|DELETE); selector-driven path: (.*)\)\.\"\"\"/m.exec(body);
    if (conditional) {
      const [, klass, method, verb, branches] = conditional;
      const paths = branches.split("; ").map((branch) => {
        const separator = branch.indexOf(": ");
        if (separator === -1) {
          throw new Error(`Cannot parse conditional path for ${klass}.${method}: ${branch}`);
        }
        return branch.slice(separator + 2);
      });
      operations.push({
        functionName,
        operation: `${klass}.${method}`,
        verb,
        paths,
        signature,
        body,
      });
      continue;
    }

    const alias = /^    \"\"\"(\w+)\.(\w+) \(alias for (\w+)\.(\w+)\)\.\"\"\"/m.exec(body);
    if (alias) {
      const [, klass, method, targetClass, targetMethod] = alias;
      aliases.push({
        operation: `${klass}.${method}`,
        target: `${targetClass}.${targetMethod}`,
      });
    }
  }

  return { operations, aliases };
}

function toolsFunctionSource(source: string, name: string): string | null {
  const start = source.indexOf(`def ${name}(`);
  if (start === -1) return null;
  const rest = source.slice(start);
  const nextTopLevel = /\n(?=@|def |class )/.exec(rest);
  return nextTopLevel?.index === undefined ? rest : rest.slice(0, nextTopLevel.index);
}

function concreteDefaultProblem(
  toolsSource: string,
  override: ConcreteDefaultOverride,
): string | null {
  const source = toolsFunctionSource(toolsSource, override.functionName);
  if (!source) return "override function no longer exists";

  const headerEnd = source.indexOf("):\n");
  if (headerEnd === -1) return "override function header cannot be parsed";
  const declaration = parameterDeclaration(
    source.slice(source.indexOf("(") + 1, headerEnd),
    override.property,
  );
  const defaultValue = declaration?.slice(declaration.lastIndexOf("=") + 1).trim();
  if (!defaultValue || defaultValue === "_UNSET" || defaultValue === "None") {
    return "no longer has a concrete default";
  }

  const assignment = new RegExp(`^    options\\[\"${escapeRegex(override.property)}\"\\] =`, "m");
  const forwardsOptions = new RegExp(
    `return _generated\\.${escapeRegex(override.functionName)}\\([\\s\\S]*?\\*\\*options`,
  );
  if (!assignment.test(source) || !forwardsOptions.test(source)) {
    return "does not always serialize its concrete default";
  }
  return null;
}

function publicUploadOverrideProblem(
  toolsSource: string,
  proof: PublicUploadOverrideProof,
  openApiOperation: RawOpenApiOperation,
): string | null {
  const source = toolsFunctionSource(toolsSource, proof.functionName);
  if (!source) return "public override function no longer exists";

  const decorator = new RegExp(
    `@_op\\(gitlab_write\\)\\s*\\ndef ${escapeRegex(proof.functionName)}\\(`,
  );
  if (!decorator.test(toolsSource)) return "public override is no longer registered in gitlab_write";

  const headerEnd = source.indexOf("):\n");
  if (headerEnd === -1) return "public override header cannot be parsed";
  const signature = source.slice(source.indexOf("(") + 1, headerEnd);
  if (signature.includes("**")) return "public override no longer has a closed signature";

  const encodedPath = proof.rawPath
    .replace(/^\/api\/v4/, "")
    .replace("{id}", "{_enc(project_id)}")
    .replace("{issue_iid}", "{_enc(issue_iid)}")
    .replace("{package_name}", "{_enc(package_name)}")
    .replace("{name}", "{_enc(name)}");
  const encodedWirePath = encodedPath + (proof.wirePathSuffix ?? "");
  if (!source.includes(`f"${encodedWirePath}"`)) {
    return "does not preserve the encoded OpenAPI path and exact wire suffix";
  }

  const filePath = parameterDeclaration(signature, "file_path");
  if (!/^file_path\s*:\s*str$/.test(filePath ?? "")) {
    return "does not expose a required file_path: str contract";
  }
  if (
    !/\bp\s*=\s*_Path\(file_path\)\.expanduser\(\)/.test(source) ||
    !/\bif not p\.exists\(\):/.test(source) ||
    !/\bif not p\.is_file\(\):/.test(source)
  ) {
    return "does not guard file_path as an existing regular file";
  }

  const legacyParameters: Record<string, readonly string[]> = {
    "Issues.uploadMetricImage": ["metric_image", "file"],
    "NPM.uploadPackageFile": ["versions", "metadata", "file"],
    "NuGet.uploadPackageFile": ["package_file", "package"],
    "NuGet.uploadSymbolPackage": ["package_file", "package"],
    "ProjectTerraformState.createVersion": ["file"],
    "RubyGems.uploadGemFile": ["package_file", "file"],
  };
  const legacy = legacyParameters[proof.operation] ?? [];
  if (legacy.some((name) => parameterDeclaration(signature, name))) {
    return "still exposes an inline-binary or synthetic file parameter";
  }

  if (proof.serializer === "multipart-file-path") {
    const multipartPart = new RegExp(
      `\\bfiles\\s*=\\s*\\{\\s*["']${escapeRegex(proof.property)}["']\\s*:\\s*\\(`,
    );
    if (!multipartPart.test(source) || !/\bp\.read_bytes\(\)/.test(source)) {
      return `does not construct multipart ${proof.property} bytes from file_path`;
    }

    const dataArguments = source.match(/\bdata\s*=/g) ?? [];
    if (dataArguments.length > 0) {
      if (
        dataArguments.length !== 1 ||
        !/\bdata\s*=\s*form\b/.test(source) ||
        !/\bform\s*:\s*dict\[str,\s*str\]\s*=\s*\{\}/.test(source) ||
        /\bform\s*\[\s*(?!["'])/.test(source) ||
        /\bform\.(?:update|setdefault)\s*\(/.test(source)
      ) {
        return "does not limit multipart auxiliary fields to proven wire names";
      }

      const provenAuxiliaryFields = new Set(
        openApiOperation.bodyFields
          .map((field) => field.name)
          .filter((field) => field !== proof.property),
      );
      const auxiliaryFields = [...source.matchAll(
        /\bform\s*\[\s*["']([^"']+)["']\s*\]\s*=/g,
      )].map((match) => match[1]);
      const unprovenAuxiliaryFields = auxiliaryFields.filter(
        (field) => !provenAuxiliaryFields.has(field),
      );
      if (unprovenAuxiliaryFields.length > 0) {
        return `serializes unproven multipart auxiliary field(s): ${unprovenAuxiliaryFields.join(", ")}`;
      }
    }
  } else {
    const contentType = proof.serializer === "raw-json-file-path"
      ? "application/json"
      : "application/octet-stream";
    const contentArguments = source.match(/\bcontent\s*=/g) ?? [];
    const contentTypeHeader = new RegExp(
      `\\bheaders\\s*=\\s*\\{[^\\n}]*["']Content-Type["']\\s*:\\s*["']${escapeRegex(contentType)}["']`,
    );
    const directContentTypeHeader = new RegExp(
      `\\._request\\(\\s*"${escapeRegex(proof.verb)}"\\s*,\\s*f"${escapeRegex(encodedWirePath)}"[\\s\\S]*?\\bcontent\\s*=\\s*p\\.read_bytes\\(\\)[\\s\\S]*?\\bheaders\\s*=\\s*\\{[^\\n}]*["']Content-Type["']\\s*:\\s*["']${escapeRegex(contentType)}["']`,
    );
    const namedContentTypeHeader = new RegExp(
      `\\._request\\(\\s*"${escapeRegex(proof.verb)}"\\s*,\\s*f"${escapeRegex(encodedWirePath)}"[\\s\\S]*?\\bcontent\\s*=\\s*p\\.read_bytes\\(\\)[\\s\\S]*?\\bheaders\\s*=\\s*headers\\b`,
    );
    if (
      contentArguments.length !== 1 ||
      (!directContentTypeHeader.test(source) &&
        (!contentTypeHeader.test(source) || !namedContentTypeHeader.test(source)))
    ) {
      return `does not send raw file_path bytes with Content-Type ${contentType}`;
    }
    if (/\bfiles\s*=/.test(source) || /\bjson\s*=/.test(source)) {
      return "uses multipart or JSON reserialization instead of the raw request body";
    }
  }

  const request = new RegExp(
    `\\._request\\(\\s*"${escapeRegex(proof.verb)}"\\s*,\\s*f"${escapeRegex(encodedWirePath)}"`,
  );
  const response = /return _ok\(None if r\.status_code == 204 or not r\.content else r\.json\(\)\)/;
  return request.test(source) && response.test(source)
    ? null
    : "does not preserve the exact HTTP verb, path, and response handling";
}

function namingOverrideProblem(
  op: GeneratedOperation,
  fieldName: string,
  pythonParameter: string,
): string | null {
  const declaration = parameterDeclaration(op.signature, pythonParameter);
  if (!declaration) return "is not exposed by the generated signature";

  const serializer = new RegExp(
    `^    payload\\[\"${escapeRegex(fieldName)}\"\\] = ${escapeRegex(pythonParameter)}$`,
    "m",
  );
  const optionalSerializer = new RegExp(
    `^    if ${escapeRegex(pythonParameter)} is not _UNSET:\n        payload\\[\"${escapeRegex(fieldName)}\"\\] = ${escapeRegex(pythonParameter)}$`,
    "m",
  );
  return serializer.test(op.body) || optionalSerializer.test(op.body)
    ? null
    : "does not serialize with its exact Python argument name";
}

function gitbeakerSourceWireNameProblem(
  implementationSource: string,
  judgment: GitbeakerSourceWireNameJudgment,
): string | null {
  const [klass, method] = judgment.operation.split(".");
  const classStart = implementationSource.indexOf(
    `var ${klass} = class extends requesterUtils.BaseResource {`,
  );
  if (classStart === -1) return "source class no longer exists";
  const classEnd = implementationSource.indexOf("\n};", classStart);
  if (classEnd === -1) return "source class has no closing boundary";

  const classSource = implementationSource.slice(classStart, classEnd);
  const methodHead = new RegExp(
    `^  ${escapeRegex(method)}\\(([^)]*)\\)\\s*\\{`,
    "m",
  ).exec(classSource);
  if (!methodHead || methodHead.index === undefined) return "source method no longer exists";
  if (!new RegExp(`\\b${escapeRegex(judgment.sourceVariable)}\\b`).test(methodHead[1])) {
    return `source no longer declares ${judgment.sourceVariable}`;
  }

  const bodyStart = methodHead.index + methodHead[0].lastIndexOf("{");
  let cursor = bodyStart + 1;
  let depth = 1;
  while (cursor < classSource.length && depth > 0) {
    if (classSource[cursor] === "{") depth++;
    else if (classSource[cursor] === "}") depth--;
    cursor++;
  }
  if (depth !== 0) return "source method has unbalanced braces";

  const methodBody = classSource.slice(bodyStart + 1, cursor - 1);
  const sourceMapping = new RegExp(
    `\\b${escapeRegex(judgment.sourceWireName)}\\s*:\\s*${escapeRegex(judgment.sourceVariable)}\\b`,
  );
  return sourceMapping.test(methodBody)
    ? null
    : `source no longer maps ${judgment.sourceVariable} to ${judgment.sourceWireName}`;
}

function gitbeakerTypeClassSource(source: string, klass: string): string | null {
  const start = source.indexOf(`declare class ${klass}<`);
  if (start === -1) return null;
  const end = source.indexOf("\n}", start);
  return end === -1 ? null : source.slice(start, end + 2);
}

function gitbeakerTypeAliasSource(source: string, alias: string): string | null {
  const start = source.indexOf(`type ${alias} = {`);
  if (start === -1) return null;
  const end = source.indexOf("\n};", start);
  return end === -1 ? null : source.slice(start, end + 3);
}

function gitbeakerTypeMethodDeclaration(
  classSource: string,
  method: string,
): string | null {
  const head = new RegExp(
    `^    ${escapeRegex(method)}(?:<[^\\n]*>)?\\(`,
    "m",
  ).exec(classSource);
  if (!head || head.index === undefined) return null;

  const open = classSource.indexOf("(", head.index);
  let cursor = open + 1;
  let depth = 1;
  while (cursor < classSource.length && depth > 0) {
    if (classSource[cursor] === "(") depth++;
    else if (classSource[cursor] === ")") depth--;
    cursor++;
  }
  return depth === 0 ? classSource.slice(head.index, cursor) : null;
}

function gitbeakerImplementationMethodSource(
  source: string,
  operation: string,
): string | null {
  const [klass, method] = operation.split(".");
  const classStart = source.indexOf(
    `var ${klass} = class extends requesterUtils.BaseResource {`,
  );
  if (classStart === -1) return null;
  const classEnd = source.indexOf("\n};", classStart);
  if (classEnd === -1) return null;

  const classSource = source.slice(classStart, classEnd);
  const methodHead = new RegExp(
    `^  ${escapeRegex(method)}\\(([^)]*)\\)\\s*\\{`,
    "m",
  ).exec(classSource);
  if (!methodHead || methodHead.index === undefined) return null;

  const bodyStart = methodHead.index + methodHead[0].lastIndexOf("{");
  let cursor = bodyStart + 1;
  let depth = 1;
  while (cursor < classSource.length && depth > 0) {
    if (classSource[cursor] === "{") depth++;
    else if (classSource[cursor] === "}") depth--;
    cursor++;
  }
  return depth === 0 ? classSource.slice(methodHead.index, cursor) : null;
}

function documentedSpecGapRationaleProblem(
  openApiOperation: RawOpenApiOperation,
  implementationSource: string,
  typesSource: string,
  gap: { operation: string; property: string },
): string | null {
  const typeClass = gitbeakerTypeClassSource(
    typesSource,
    gap.operation.split(".")[0],
  );
  const typeMethod = typeClass
    ? gitbeakerTypeMethodDeclaration(typeClass, gap.operation.split(".")[1])
    : null;
  const implementationMethod = gitbeakerImplementationMethodSource(
    implementationSource,
    gap.operation,
  );
  const key = `${gap.operation} ${gap.property}`;

  if (
    key === "RepositoryFiles.create file" ||
    key === "RepositoryFiles.edit file"
  ) {
    if (
      !typeMethod ||
      !/\bbranch\s*:\s*string\b/.test(typeMethod) ||
      !/\bcontent\s*:\s*string\b/.test(typeMethod) ||
      !/\bcommitMessage\s*:\s*string\b/.test(typeMethod) ||
      /\bfile\s*:/.test(typeMethod)
    ) {
      return "GitBeaker no longer exposes the branch/content/commitMessage file JSON contract";
    }
    if (
      !implementationMethod ||
      !/\bbranch\s*,\s*\bcontent\s*,\s*\bcommitMessage\s*,\s*\.\.\.options/.test(
        implementationMethod,
      ) ||
      /\bfile\s*:/.test(implementationMethod)
    ) {
      return "GitBeaker no longer constructs the branch/content/commitMessage file JSON contract";
    }
  }

  if (key === "Issues.uploadMetricImage file") {
    if (
      !typeMethod ||
      !/\bmetricImage\s*:\s*\{[\s\S]*?\bcontent\s*:\s*Blob;[\s\S]*?\bfilename\s*:\s*string;/.test(
        typeMethod,
      )
    ) {
      return "GitBeaker no longer exposes the metricImage content/filename contract";
    }
    if (
      !implementationMethod ||
      !/\bisForm\s*:\s*true/.test(implementationMethod) ||
      !/\bfile\s*:\s*\[\s*metricImage\.content\s*,\s*metricImage\.filename\s*\]/.test(
        implementationMethod,
      )
    ) {
      return "GitBeaker no longer constructs the metric-image multipart contract";
    }
  }

  if (
    key === "NuGet.uploadPackageFile package" ||
    key === "NuGet.uploadSymbolPackage package"
  ) {
    if (
      !typeMethod ||
      !/\bpackageFile\s*:\s*\{[\s\S]*?\bcontent\s*:\s*Blob;[\s\S]*?\bfilename\s*:\s*string;/.test(
        typeMethod,
      )
    ) {
      return "GitBeaker no longer exposes the NuGet packageFile content/filename contract";
    }
    if (
      !implementationMethod ||
      !/\bisForm\s*:\s*true/.test(implementationMethod) ||
      !/\bpackageName\b/.test(implementationMethod) ||
      !/\bpackageVersion\b/.test(implementationMethod) ||
      !/\bfile\s*:\s*\[\s*packageFile\.content\s*,\s*packageFile\.filename\s*\]/.test(
        implementationMethod,
      )
    ) {
      return "GitBeaker no longer constructs the NuGet multipart package contract";
    }
  }

  if (key === "ProjectTerraformState.createVersion file") {
    if (!typeMethod || /\bfile\s*:/.test(typeMethod)) {
      return "GitBeaker no longer exposes the no-file Terraform state type contract";
    }
    if (
      !implementationMethod ||
      !/RequestHelper\.post\(\)/.test(implementationMethod) ||
      !/\bcreateVersion\(projectId,\s*name,\s*options\)/.test(implementationMethod) ||
      /\bfile\s*:/.test(implementationMethod)
    ) {
      return "GitBeaker no longer forwards the untyped Terraform state options contract";
    }
  }

  if (key === "RubyGems.uploadGemFile file") {
    if (
      !typeMethod ||
      !/\bpackageFile\s*:\s*\{[\s\S]*?\bcontent\s*:\s*Blob;[\s\S]*?\bfilename\s*:\s*string;/.test(
        typeMethod,
      )
    ) {
      return "GitBeaker no longer exposes the RubyGems packageFile content/filename contract";
    }
    if (
      !implementationMethod ||
      !/\bisForm\s*:\s*true/.test(implementationMethod) ||
      !/\bfile\s*:\s*\[\s*packageFile\.content\s*,\s*packageFile\.filename\s*\]/.test(
        implementationMethod,
      )
    ) {
      return "GitBeaker no longer constructs the RubyGems multipart package contract";
    }
  }

  if (key === "Commits.create file") {
    if (!typeMethod || /\bfile\s*:/.test(typeMethod)) {
      return "GitBeaker no longer exposes the no-file commit type contract";
    }
    if (
      !implementationMethod ||
      !/\bbranch\b/.test(implementationMethod) ||
      !/\bcommitMessage\s*:\s*message\b/.test(implementationMethod) ||
      !/\bactions\b/.test(implementationMethod) ||
      /\bfile\s*:/.test(implementationMethod)
    ) {
      return "GitBeaker no longer constructs the branch/commitMessage/actions JSON contract";
    }
  }

  if (
    key === "Commits.createComment line" ||
    key === "Commits.createComment line_type"
  ) {
    if (
      !typeMethod ||
      !/\bpath\s*\?:/.test(typeMethod) ||
      !/\bline\s*\?:/.test(typeMethod) ||
      !/\blineType\s*\?:/.test(typeMethod)
    ) {
      return "GitBeaker no longer exposes optional inline-comment selectors";
    }
    if (!implementationMethod || !/\.\.\.options\b/.test(implementationMethod)) {
      return "GitBeaker no longer forwards optional inline-comment selectors";
    }
  }

  if (key === "NPM.uploadPackageFile file") {
    if (!typeMethod || /\bfile\s*:/.test(typeMethod)) {
      return "GitBeaker no longer exposes the metadata-only NPM publish type contract";
    }
    if (
      !implementationMethod ||
      !/\bversions\b/.test(implementationMethod) ||
      !/\.\.\.metadata\b/.test(implementationMethod) ||
      /\bfile\s*:/.test(implementationMethod)
    ) {
      return "GitBeaker no longer constructs the metadata-only NPM publish contract";
    }
  }

  if (key === "ProjectImportExports.importRemoteS3 url") {
    const sourceParameters = [
      "accessKeyId",
      "bucketName",
      "fileKey",
      "path",
      "region",
      "secretAccessKey",
    ];
    if (
      !typeMethod ||
      sourceParameters.some((name) => !new RegExp(`\\b${name}\\s*:`).test(typeMethod)) ||
      /\burl\s*:/.test(typeMethod)
    ) {
      return "GitBeaker no longer exposes the S3-only import positional contract";
    }
    if (
      !implementationMethod ||
      sourceParameters.some((name) => !new RegExp(`\\b${name}\\b`).test(implementationMethod)) ||
      /\burl\s*:/.test(implementationMethod)
    ) {
      return "GitBeaker no longer constructs the S3-only import contract";
    }
  }

  if (
    key === "ProjectSnippets.create file_name" ||
    key === "Snippets.create file_name"
  ) {
    const snippetOptions = gitbeakerTypeAliasSource(typesSource, "CreateSnippetOptions");
    if (!openApiOperation.bodyFields.some((field) => field.name === "files")) {
      return "OpenAPI no longer admits the files alternative";
    }
    if (
      !typeMethod ||
      !/\boptions\s*\?:\s*CreateSnippetOptions\b/.test(typeMethod) ||
      !snippetOptions ||
      !/\bfiles\s*\?:/.test(snippetOptions) ||
      !/\bfilePath\s*:/.test(snippetOptions) ||
      !/\bcontent\s*:/.test(snippetOptions)
    ) {
      return "GitBeaker no longer exposes the files alternative";
    }
    if (!implementationMethod || !/\.\.\.options\b/.test(implementationMethod)) {
      return "GitBeaker no longer forwards the files alternative";
    }
  }

  if (
    key === "Users.createCIRunner group_id" ||
    key === "Users.createCIRunner project_id"
  ) {
    const runnerOptions = gitbeakerTypeAliasSource(typesSource, "CreateUserCIRunnerOptions");
    if (!openApiOperation.bodyFields.some(
      (field) => field.name === "runner_type" && field.required,
    )) {
      return "OpenAPI no longer identifies runner_type as the required scope selector";
    }
    if (
      !typeMethod ||
      !/\brunnerType\s*:\s*'instance_type'\s*\|\s*'group_type'\s*\|\s*'project_type'/.test(typeMethod) ||
      !/\boptions\s*\?:\s*CreateUserCIRunnerOptions\b/.test(typeMethod) ||
      !runnerOptions ||
      !/\bgroupId\s*\?:/.test(runnerOptions) ||
      !/\bprojectId\s*\?:/.test(runnerOptions)
    ) {
      return "GitBeaker no longer exposes optional runner scopes selected by runner_type";
    }
    if (!implementationMethod || !/\.\.\.options\b/.test(implementationMethod)) {
      return "GitBeaker no longer forwards optional runner scopes";
    }
  }

  return null;
}

function conditionalBranchFieldProblem(
  op: GeneratedOperation,
  judgment: ConditionalBranchFieldJudgment,
): string | null {
  const pyName = pythonName(judgment.property);
  if (!parameterDeclaration(op.signature, pyName)) {
    return "is not exposed by the generated signature";
  }

  const serializer = `payload["${judgment.property}"] = ${pyName}`;
  const serializations = op.body.split(serializer).length - 1;
  if (serializations !== 1) {
    return `has ${serializations} serializers instead of exactly one`;
  }

  const selector = toSnake(judgment.selectorParameter);
  const branch = new RegExp(
    `^    if ${escapeRegex(selector)}:\\n(?:        [^\\n]*\\n)*?        if ${escapeRegex(pyName)} is not _UNSET:\\n            ${escapeRegex(serializer)}$`,
    "m",
  );
  return branch.test(op.body)
    ? null
    : `is not serialized only under ${selector}`;
}

function requiredBodyProblem(
  op: GeneratedOperation,
  field: { name: string; pyName: string; allowNull: boolean },
): string | null {
  const declaration = parameterDeclaration(op.signature, field.pyName);
  if (!declaration) return "is not exposed by the generated signature";

  const defaultIndex = declaration.lastIndexOf("=");
  if (defaultIndex !== -1) {
    const defaultValue = declaration.slice(defaultIndex + 1).trim();
    if (defaultValue === "_UNSET") {
      const guardedSerializer = new RegExp(
        `^    if ${escapeRegex(field.pyName)} is not _UNSET:\n        payload\\[\"${escapeRegex(field.name)}\"\\] = ${escapeRegex(field.pyName)}$`,
        "m",
      );
      return guardedSerializer.test(op.body)
        ? "may be omitted with _UNSET"
        : "has an _UNSET default without an auditable serializer";
    }
    if (defaultValue === "None") return "may be omitted with None";
    return `has unaudited default ${defaultValue}`;
  }

  const serializer = new RegExp(
    `^    payload\\[\"${escapeRegex(field.name)}\"\\] = ${escapeRegex(field.pyName)}$`,
    "m",
  );
  if (!serializer.test(op.body)) return "is required but not unconditionally serialized";

  const payloadKind = op.verb === "GET" || op.verb === "DELETE" ? "params" : "json";
  const request = new RegExp(
    `return _ok\\(_get_client\\(\\)\\.request\\(\"${escapeRegex(op.verb)}\", [^\\n]+, ${payloadKind}=payload\\)\\)`,
  );
  if (!request.test(op.body)) return `is not serialized as ${payloadKind}=payload`;

  const acceptsNone = /\|\s*None\b/.test(declaration);
  if (field.allowNull) {
    return acceptsNone ? null : "is allow-null but does not accept None";
  }
  if (acceptsNone) return "accepts None without an exact allow-null judgment";

  const nonNullGuard = new RegExp(
    `^    if ${escapeRegex(field.pyName)} is None:\n        raise ValueError\\([^\\n]+\\)\n    payload\\[\"${escapeRegex(field.name)}\"\\] = ${escapeRegex(field.pyName)}$`,
    "m",
  );
  return nonNullGuard.test(op.body)
    ? null
    : "does not reject None before serialization";
}

function literalHygieneProblems(source: string): string[] {
  const problems: string[] = [];
  for (const match of source.matchAll(/Literal\[([^\]]*)\]/g)) {
    const members = match[1].split(", ").filter(Boolean);
    const canonical = members.map((member) => {
      if (!member.startsWith("'") || !member.endsWith("'")) return member;
      return `"${member.slice(1, -1)}"`;
    });
    if (members.some((member) => member.startsWith("'"))) {
      problems.push(`mixed quote style in ${match[0]}`);
    }
    if (new Set(canonical).size !== canonical.length) {
      problems.push(`duplicate values in ${match[0]}`);
    }
  }
  return problems;
}

const rawOpenApi = rawOpenApiOperations(readFileSync(OPENAPI_PATH, "utf-8"));
const generatedSource = readFileSync(GENERATED_PATH, "utf-8");
const generated = generatedFunctions(generatedSource);
const literalHygiene = literalHygieneProblems(generatedSource);
if (literalHygiene.length > 0) {
  throw new Error(
    `Generated Literal annotations must use unique double-quoted values:\n${literalHygiene
      .map((problem) => `  ${problem}`)
      .join("\n")}`,
  );
}
const toolsSource = readFileSync(TOOLS_PATH, "utf-8");
const gitbeakerImplementationSource = readFileSync(
  GITBEAKER_IMPLEMENTATION_PATH,
  "utf-8",
);
const gitbeakerTypesSource = readFileSync(GITBEAKER_TYPES_PATH, "utf-8");
const canonical = new Set(generated.operations.map((op) => op.operation));
for (const alias of generated.aliases) {
  if (!canonical.has(alias.target)) {
    throw new Error(`${alias.operation} aliases missing canonical operation ${alias.target}`);
  }
}

const failures = new Set<string>();
const appliedSpecGaps = new Set<string>();
const appliedBodyOverrides = new Set<string>();
const appliedConcreteDefaults = new Set<string>();
const appliedConditionalBranchFields = new Set<string>();
const appliedGitbeakerSourceWireNameJudgments = new Set<string>();
const appliedPublicUploadOverrideProofs = new Set<string>();
let joinedOperations = 0;
for (const operation of generated.operations) {
  for (const path of operation.paths) {
    const openApiOperation = rawOpenApi.get(
      conformanceKey(operation.verb, path.split("?", 1)[0]),
    );
    if (!openApiOperation) continue;
    joinedOperations++;

    for (const field of openApiOperation.bodyFields) {
      const override = bodyFieldOverride(
        operation.operation,
        operation.verb,
        openApiOperation.rawPath,
        field.name,
      );
      if (override) {
        appliedBodyOverrides.add(bodyFieldJudgmentKey(override));
        if (override.pyName || override.sourceParameter) {
          const problem = namingOverrideProblem(
            operation,
            field.name,
            override.pyName ?? pythonName(field.name),
          );
          if (problem) {
            failures.add(
              `${operation.operation}: exact body-field override ${field.name} ${problem} (${operation.verb} ${openApiOperation.rawPath})`,
            );
          }
        }
      }
      if (!field.required) continue;

      const gap = documentedSpecGap(
        operation.operation,
        operation.verb,
        openApiOperation.rawPath,
        field.name,
      );
      if (gap) {
        appliedSpecGaps.add(bodyFieldJudgmentKey(gap));
        const problem = documentedSpecGapRationaleProblem(
          openApiOperation,
          gitbeakerImplementationSource,
          gitbeakerTypesSource,
          gap,
        );
        if (problem) {
          failures.add(
            `${operation.operation}: documented spec gap ${field.name} ${problem} (${operation.verb} ${openApiOperation.rawPath})`,
          );
        }
        continue;
      }

      const concreteDefault = concreteDefaultOverride(
        operation.operation,
        operation.verb,
        openApiOperation.rawPath,
        field.name,
      );
      if (concreteDefault) {
        appliedConcreteDefaults.add(bodyFieldJudgmentKey(concreteDefault));
        if (concreteDefault.functionName !== operation.functionName) {
          failures.add(
            `${operation.operation}: concrete default for ${field.name} targets ${concreteDefault.functionName}, not ${operation.functionName}`,
          );
          continue;
        }
        const problem = concreteDefaultProblem(toolsSource, concreteDefault);
        if (problem) {
          failures.add(
            `${operation.operation}: concrete default for ${field.name} ${problem} (${operation.verb} ${openApiOperation.rawPath})`,
          );
        }
        continue;
      }

      const problem = requiredBodyProblem(operation, {
        name: field.name,
        pyName: override?.pyName ?? pythonName(field.name),
        allowNull: override?.allowNull === true,
      });
      if (problem) {
        failures.add(
          `${operation.operation}: required body field ${field.name} ${problem} (${operation.verb} ${openApiOperation.rawPath})`,
        );
      }
    }
  }
}

// A documented source/spec divergence is not enough for an exposed upload:
// each public replacement must retain its exact closed signature and wire shape.
for (const proof of PUBLIC_UPLOAD_OVERRIDE_PROOFS) {
  const operation = generated.operations.find(
    (candidate) =>
      candidate.operation === proof.operation &&
      candidate.verb === proof.verb,
  );
  if (!operation) continue;
  const path = operation.paths.find(
    (candidate) =>
      conformancePath(candidate) === conformancePath(proof.rawPath),
  );
  if (!path) continue;
  const openApiOperation = rawOpenApi.get(
    conformanceKey(operation.verb, path.split("?", 1)[0]),
  );
  if (!openApiOperation?.bodyFields.some(
    (field) => field.name === proof.property && field.required,
  )) {
    continue;
  }

  appliedPublicUploadOverrideProofs.add(bodyFieldJudgmentKey(proof));
  const problem = publicUploadOverrideProblem(toolsSource, proof, openApiOperation);
  if (problem) {
    failures.add(
      `${proof.operation}: public upload override ${proof.functionName} ${problem} (${proof.verb} ${proof.rawPath})`,
    );
  }
}

// Stale-check exact GitBeaker positional-to-wire mappings and ensure the generated
// wrapper exposes the judgment's canonical caller/wire contract.
for (const judgment of GITBEAKER_SOURCE_WIRE_NAME_JUDGMENTS) {
  const operation = generated.operations.find(
    (candidate) =>
      candidate.operation === judgment.operation &&
      candidate.verb === judgment.verb,
  );
  if (!operation) continue;
  const path = operation.paths.find(
    (candidate) =>
      conformancePath(candidate) === conformancePath(judgment.rawPath),
  );
  if (!path) continue;
  const openApiOperation = rawOpenApi.get(
    conformanceKey(operation.verb, path.split("?", 1)[0]),
  );
  if (
    !openApiOperation ||
    openApiOperation.bodyFields.some((field) => field.name === judgment.wireName)
  ) continue;

  appliedGitbeakerSourceWireNameJudgments.add(sourceWireNameJudgmentKey(
    judgment.operation,
    judgment.verb,
    judgment.rawPath,
    judgment.sourceParameter,
  ));
  const generatedProblem = namingOverrideProblem(
    operation,
    judgment.wireName,
    judgment.callerName ?? judgment.sourceParameter,
  );
  if (generatedProblem) {
    failures.add(
      `${operation.operation}: GitBeaker source wire-name judgment ${judgment.wireName} ${generatedProblem} (${operation.verb} ${judgment.rawPath})`,
    );
  }
  const sourceProblem = gitbeakerSourceWireNameProblem(
    gitbeakerImplementationSource,
    judgment,
  );
  if (sourceProblem) {
    failures.add(
      `${operation.operation}: GitBeaker source wire-name judgment ${judgment.wireName} ${sourceProblem} (${operation.verb} ${judgment.rawPath})`,
    );
  }
}

// This verifies only the exact branch-local fields listed in the judgment
// registry; it intentionally does not claim exhaustive optional-field coverage.
for (const judgment of CONDITIONAL_BRANCH_FIELD_JUDGMENTS) {
  const operation = generated.operations.find(
    (candidate) =>
      candidate.operation === judgment.operation &&
      candidate.verb === judgment.verb,
  );
  if (!operation) continue;
  const path = operation.paths.find(
    (candidate) =>
      conformancePath(candidate) === conformancePath(judgment.rawPath),
  );
  if (!path) continue;
  const openApiOperation = rawOpenApi.get(
    conformanceKey(operation.verb, path.split("?", 1)[0]),
  );
  if (!openApiOperation?.bodyFields.some((field) => field.name === judgment.property)) {
    continue;
  }
  appliedConditionalBranchFields.add(bodyFieldJudgmentKey(judgment));
  const problem = conditionalBranchFieldProblem(operation, judgment);
  if (problem) {
    failures.add(
      `${operation.operation}: conditional branch field ${judgment.property} ${problem} (${operation.verb} ${judgment.rawPath})`,
    );
  }
}

const staleSpecGaps = DOCUMENTED_SPEC_GAPS.filter(
  (gap) => !appliedSpecGaps.has(bodyFieldJudgmentKey(gap)),
);
if (staleSpecGaps.length > 0) {
  throw new Error(
    `Stale required-body spec gaps:\n${staleSpecGaps
      .map((gap) => `  ${gap.operation}: ${gap.verb} ${gap.rawPath} ${gap.property} - ${gap.rationale}`)
      .join("\n")}`,
  );
}

const stalePublicUploadOverrideProofs = PUBLIC_UPLOAD_OVERRIDE_PROOFS.filter(
  (proof) => !appliedPublicUploadOverrideProofs.has(bodyFieldJudgmentKey(proof)),
);
if (stalePublicUploadOverrideProofs.length > 0) {
  throw new Error(
    `Stale public upload override proofs:\n${stalePublicUploadOverrideProofs
      .map((proof) => `  ${proof.operation}: ${proof.verb} ${proof.rawPath} ${proof.functionName}`)
      .join("\n")}`,
  );
}

const staleBodyOverrides = BODY_FIELD_OVERRIDES.filter(
  (override) => !appliedBodyOverrides.has(bodyFieldJudgmentKey(override)),
);
if (staleBodyOverrides.length > 0) {
  throw new Error(
    `Stale body-field overrides:\n${staleBodyOverrides
      .map((override) => `  ${override.operation}: ${override.verb} ${override.rawPath} ${override.property}`)
      .join("\n")}`,
  );
}

const staleConcreteDefaults = CONCRETE_DEFAULT_OVERRIDES.filter(
  (override) => !appliedConcreteDefaults.has(bodyFieldJudgmentKey(override)),
);
if (staleConcreteDefaults.length > 0) {
  throw new Error(
    `Stale concrete-default overrides:\n${staleConcreteDefaults
      .map((override) => `  ${override.operation}: ${override.verb} ${override.rawPath} ${override.property}`)
      .join("\n")}`,
  );
}

const staleConditionalBranchFields = CONDITIONAL_BRANCH_FIELD_JUDGMENTS.filter(
  (judgment) => !appliedConditionalBranchFields.has(bodyFieldJudgmentKey(judgment)),
);
if (staleConditionalBranchFields.length > 0) {
  throw new Error(
    `Stale conditional branch field judgments:\n${staleConditionalBranchFields
      .map((judgment) => `  ${judgment.operation}: ${judgment.verb} ${judgment.rawPath} ${judgment.property} - ${judgment.rationale}`)
      .join("\n")}`,
  );
}

const staleGitbeakerSourceWireNameJudgments = GITBEAKER_SOURCE_WIRE_NAME_JUDGMENTS.filter(
  (judgment) => !appliedGitbeakerSourceWireNameJudgments.has(
    sourceWireNameJudgmentKey(
      judgment.operation,
      judgment.verb,
      judgment.rawPath,
      judgment.sourceParameter,
    ),
  ),
);
if (staleGitbeakerSourceWireNameJudgments.length > 0) {
  throw new Error(
    `Stale GitBeaker source wire-name judgments:\n${staleGitbeakerSourceWireNameJudgments
      .map((judgment) => `  ${judgment.operation}: ${judgment.verb} ${judgment.rawPath} ${judgment.sourceParameter} -> ${judgment.wireName} - ${judgment.rationale}`)
      .join("\n")}`,
  );
}

if (joinedOperations === 0) {
  throw new Error("Required-body conformance check joined no generated operations to raw OpenAPI");
}
if (failures.size > 0) {
  console.error("Required request-body fields must not be omittable or misleadingly nullable:");
  for (const failure of [...failures].sort()) console.error(`  ${failure}`);
  process.exitCode = 1;
}
