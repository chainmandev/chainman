# Trusted bootstrap companion; it parses data without importing consumer code.
{
  root,
  authority ? root,
  action ? "fetch",
  archive ? "",
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
  raw = b.fromJSON (b.readFile (authority + "/chainman.lock"));
  lock =
    if
      raw.schema or null != 1
      || !(b.all (key: raw ? ${key} && b.isString raw.${key}) [
        "version"
        "revision"
        "url"
        "narHash"
      ])
      || b.match "sha256-[A-Za-z0-9+/]{43}=" raw.narHash == null
      || b.match "https://.+" raw.url == null
    then
      fail "chainman.lock requires schema 1, version, revision, HTTPS url, and SHA256 SRI narHash"
    else
      b.deepSeq (map line [
        raw.version
        raw.revision
        raw.url
        raw.narHash
      ]) raw;
  bundled = lock.bundled_archive or "";
  local =
    if archive != "" then
      line archive
    else if bundled == "" then
      ""
    else if
      b.match "[^/].*" (line bundled) == null
      || b.any (part: part == ".." || part == "." || part == "") (
        b.filter b.isString (b.split "/" bundled)
      )
    then
      fail "bundled_archive must be a contained consumer-relative file"
    else
      authority + "/" + bundled;
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
else if action == "metadata" then
  lines [
    (b.hashString "sha256" lock.narHash)
    lock.narHash
    (if local == "" then "-" else local)
  ]
else if action == "fetch" then
  b.seq lock (
    toString (
      b.fetchTarball {
        # Importing bytes is not executing them. A store filename also avoids URL
        # escaping ambiguities for local archive names containing spaces or '#'.
        url =
          if local == "" then
            lock.url
          else
            b.unsafeDiscardStringContext "file://${
              b.path {
                path = b.toPath local;
                name = "chainman-archive";
              }
            }";
        sha256 = lock.narHash;
      }
    )
  )
else
  fail "unknown bootstrap action"
