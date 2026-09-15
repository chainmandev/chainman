# Runtime-owned configuration projection; never import consumer code here.
{
  root,
  authority ? root,
  action ? "fetch",
  source,
}:
let
  b = builtins;
  fail = message: throw "Chainman bootstrap: ${message}";
  line =
    value:
    if b.isString value && value != "" && b.match ".*[\n\r].*" value == null then
      value
    else
      fail "expected a nonempty single-line string";
  lines = values: b.concatStringsSep "\n" (map line values) + "\n";
  config =
    if b.pathExists (authority + "/chainman.toml") then
      b.fromTOML (b.readFile (authority + "/chainman.toml"))
    else
      { };
  pattern =
    value:
    if b.match "[A-Za-z_][A-Za-z0-9_*?]*" (line value) == null then
      fail "environment.pass accepts environment names and * or ? patterns"
    else
      [
        "--env-pattern"
        value
      ];
  mount =
    item:
    let
      dynamic = item ? source_env;
      source = if dynamic then line item.source_env else line item.source;
      target = if dynamic && !(item ? target) then "" else line item.target;
      absolute = if b.substring 0 1 source == "/" then source else root + "/" + source;
      readOnly = item.read_only or true;
    in
    if
      (item ? source) == dynamic
      || b.any (
        name:
        !(b.elem name [
          "source"
          "source_env"
          "target"
          "read_only"
        ])
      ) (b.attrNames item)
      || b.match ".*,.*" (source + target) != null
      || (target != "" && b.substring 0 1 target != "/")
      || !(b.isBool readOnly)
      || (
        dynamic
        && (
          b.match "[A-Za-z_][A-Za-z0-9_]*" source == null
          || b.match "(CHAINMAN|TOOLCHAIN)_.*" source != null
          || b.elem source [
            "SCCACHE_SERVER_UDS"
            "RUSTC_WRAPPER"
          ]
        )
      )
    then
      fail "container mounts require one source/source_env, an absolute target, and boolean read_only"
    else if dynamic then
      [
        "--mount-env"
        "${source}:${target}:${if readOnly then "ro" else "rw"}"
      ]
    else
      [
        "--mount"
        "type=bind,src=${absolute},dst=${target}${if readOnly then ",readonly" else ""}"
      ];
  request = b.getEnv "CHAINMAN_REQUEST_ACTION";
  requestedName = b.getEnv "CHAINMAN_REQUEST_TASK";
  transport =
    if request == "_workflow-service" || request == "_workflow-probe" then
      config.services.${requestedName}.transport or { }
    else if request == "_workflow-task" || request == "run" then
      config.tasks.${requestedName}.transport or { }
    else
      config.tasks.${request}.transport or { };
  containerOptions =
    item:
    b.concatMap mount (item.mounts or [ ])
    ++ b.concatMap (port: [
      "--publish"
      (line port)
    ]) (item.ports or [ ])
    ++ (
      if item.host_access or false then
        [
          "--add-host"
          "host.docker.internal:host-gateway"
        ]
      else
        [ ]
    );
  options =
    b.concatMap pattern (config.environment.pass or [ ])
    # Context values originate in checked-in task declarations. Their names must
    # also cross subsequent service/build container entries, without forwarding
    # an undeclared wildcard or installing host inputs in the planner process.
    ++ b.concatMap (task: b.concatMap pattern (b.attrNames (task.context_environment or { }))) (
      b.attrValues (config.tasks or { })
    )
    ++ containerOptions (config.container or { })
    ++ containerOptions transport;
  requestedTask = if request == "run" then b.getEnv "CHAINMAN_REQUEST_TASK" else request;
  hasServices =
    visited: name:
    if b.elem name visited then
      fail "workflow dependency cycle"
    else
      let
        task = config.tasks.${name} or { };
      in
      (task.services or [ ]) != [ ] || b.any (hasServices (visited ++ [ name ])) (task.depends_on or [ ]);
  controller =
    if
      b.elem request [
        "services-status"
        "services-stop"
        "services-run"
        "services-up"
        "services-reset"
      ]
    then
      true
    else if
      b.elem request [
        "_control-export"
        "_workflow-task"
        "_workflow-service"
        "_workflow-prepare"
        "_service-prepare"
        "exec"
        "shell"
        "version"
        "doctor"
        "deps-query"
        "deps-update"
        "chainman-update"
        "deps-check"
        "clean"
        "cache-prune"
        "cache-status"
      ]
    then
      false
    else
      config.schema or 1 == 2 && hasServices [ ] requestedTask;
  controlOnly = b.elem request [
    "_control-export"
    "services-status"
    "services-stop"
  ];
in
if action == "schema" then
  toString (config.schema or 1)
else if action == "route" then
  (if controller then "1" else "0")
else if action == "options" then
  (
    if controller then
      lines [
        "--controller"
        "1"
      ]
    else
      ""
  )
  + (if controlOnly || options == [ ] then "" else lines options)
else if action == "fetch" then
  toString (
    b.path {
      path = b.toPath source;
      name = "chainman-source";
    }
  )
else
  fail "unknown bootstrap action"
