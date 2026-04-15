/**
 * Gitbeaker resource classes that are NOT available on Heptapod
 * (EE-only) or that we deliberately skip in v1 (non-core package registries,
 * utility wrappers, etc.).
 *
 * Any class whose name appears here is fully excluded from codegen.
 */

export const EE_EXCLUDED_CLASSES: Set<string> = new Set([
  // EE-only
  "Epics",
  "EpicAwardEmojis",
  "EpicDiscussions",
  "EpicIssues",
  "EpicLabelEvents",
  "EpicLinks",
  "EpicNotes",
  "Iterations",
  "IterationCadences",
  "Vulnerabilities",
  "VulnerabilityExports",
  "VulnerabilityFindings",
  "Requirements",
  "RequirementsManagement",
  "PushRules",
  "GroupPushRules",
  // MergeRequestApprovals — approve/unapprove are CE but approval RULES are EE.
  // Keep the class (approve/unapprove are useful), only skip rule-management
  // methods via codegen if needed.
  "GroupMergeRequestApprovals",
  "ProtectedEnvironments",
  "ResourceProtectedEnvironments",
  "ExternalStatusChecks",
  "StatusChecks",
  "AuditEvents",
  "GroupAuditEvents",
  "ProjectAuditEvents",
  "InstanceAuditEvents",
  "Dependencies",
  "LicenseManagement",
  "CodeSuggestions",
  "ValueStreamAnalytics",
  "Insights",
  "GeoNodes",
  "GeoSites",
  "ManagedLicenses",
  "AlertManagement",
  "DashboardAnnotations",
  "FreezePeriods",
  "Experiments",
  "DevOpsAdoption",

  // Package registries we don't surface in v1
  "Conan",
  "Debian",
  "Composer",
  "Nuget",
  "Npm",
  "Pypi",
  "Maven",
  "Helm",
  "GenericPackages",
  "TerraformModuleRegistry",
  "Go",
  "Rpm",
  "Rubygems",
  "Ml",
  "MlModelRegistry",

  // Resource* base classes with EE-only semantics. Other Resource* bases
  // (Members, Labels, Hooks, Notes, …) are parsed by the main loop and
  // expanded onto concrete subclasses (ProjectMembers, GroupLabels, …) in
  // generate.ts's second pass.
  "ResourceProtectedEnvironments",
  "ResourceDORA4Metrics",
  "ResourcePushRules",

  // Concrete subclasses that we don't want to generate
  "EpicAwardEmojis",
  "EpicDiscussions",
  "EpicLabelEvents",
  "EpicNotes",
  "ProjectDORA4Metrics",
  "GroupDORA4Metrics",
]);
