# Trusted bootstrap companion; it parses data without importing consumer code.
{
  root,
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
  raw = b.fromJSON (b.readFile (root + "/chainman.lock"));
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
      root + "/" + bundled;
  config =
    if b.pathExists (root + "/chainman.toml") then
      b.fromTOML (b.readFile (root + "/chainman.toml"))
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
      source = line item.source;
      target = line item.target;
      absolute = if b.substring 0 1 source == "/" then source else root + "/" + source;
    in
    if
      b.match ".*,.*" (source + target) != null
      || b.substring 0 1 target != "/"
      || !(b.isBool (item.read_only or true))
    then
      fail "container mounts require comma-free source, absolute target, and boolean read_only"
    else
      [
        "--mount"
        "type=bind,src=${absolute},dst=${target}${if item.read_only or true then ",readonly" else ""}"
      ];
  request = b.getEnv "CHAINMAN_REQUEST_ACTION";
  requestedName = b.getEnv "CHAINMAN_REQUEST_TASK";
  transport =
    if request == "_workflow-service" then
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
      ]
    then
      true
    else if
      b.elem request [
        "_control-export"
        "_workflow-task"
        "_workflow-service"
        "_workflow-prepare"
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
if action == "route" then
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
