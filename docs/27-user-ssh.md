# Per-user outbound SSH terminals

ANSI terminal callers can press **V** on the main menu to manage and open
their own SSH connection profiles. Each profile belongs to exactly one BBS
account and stores only a label, hostname, port, remote username, and the
trusted server host-key fingerprint.

## Security behavior

- Remote passwords are masked, retained only for the connection attempt, and
  never stored in the database or logs.
- The first connection shows the server's SHA-256 host-key fingerprint and
  requires explicit confirmation. Later connections fail if that key changes.
- Regular callers can connect only to globally routable IP addresses. This
  blocks loopback, private, link-local, multicast, and reserved targets so a
  caller cannot use the BBS as a bridge into its host network.
- SysOp accounts may connect to private/LAN targets intentionally.
- NetBird destinations are a hard exception: the complete `100.64.0.0/10`
  CGNAT allocation (including this host's `wt0` network at
  `100.111.0.0/16`) is blocked for every caller, including SysOps. A hostname
  is rejected if any of its DNS answers falls in that range.
- The resolver's chosen IP is passed directly to AsyncSSH, preventing a second
  DNS lookup from switching to a blocked address after validation.
- Press **Ctrl+]**, then **Q** to close the remote session and return to ANetBBS.

Private-key storage and agent forwarding are intentionally not supported. If a
server requires key-only authentication, create a restricted account which
also permits password authentication, or use a future external secret-provider
integration rather than copying private keys into the BBS database.

The SysOp can move or remove the menu entry through **Admin → BBS Menus** using
the `ssh_client` action type. Profiles are deleted automatically when their BBS
user is deleted.
