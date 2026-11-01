# Offline container inventory and containment — approval-only

No command in this document has been run on Spark1. No live container names,
data root, installed units, or complete launcher inventory are established.
Repository Docker scripts use `--restart unless-stopped`; this is evidence of a
possible system-daemon boot path, NOT an inventory of deployed containers.

## Pre-boot gate

A user-manager mask alone is insufficient. The separately approved one-time
emergency boot must also mask `docker.service`, `docker.socket` and
`containerd.service` at the kernel command line before any normal target starts:

    systemd.unit=emergency.target systemd.mask=user@1000.service systemd.mask=cron.service systemd.mask=docker.service systemd.mask=docker.socket systemd.mask=containerd.service

UID 1000 is an unverified current identity: confirm at console before persistent
changes. These are the known standard unit names, NOT proof that custom daemon
units, alternate sockets, rootless runtimes, kubelet, Podman/Quadlet or scripts are
contained. Stay in emergency mode pending their inventory. If the editable boot
entry or authenticated emergency shell is unavailable, STOP; do not normal-boot
or select NVIDIA factory recovery (which erases the SSD).

This temporarily prevents ALL Docker/containerd workloads on the host, including
unrelated application/database/network/monitoring containers and Kubernetes
consumers, not merely media. Their identities and downtime impact are unknown.
The owner must approve that broad temporary boot containment explicitly. No permission
to kill an already-running workload, edit container metadata, or erase anything is
included. If unexpected daemon/workload processes already exist, STOP for exact
disposition rather than assuming the masks stopped them.

## Read-only offline inventory at the approved console

1. Confirm emergency target, masks and inactive units using `systemctl show`
   (`LoadState`, `ActiveState`, `SubState`, `FragmentPath`, `DropInPaths`) and
   `systemctl is-active` for the named units. These query PID1, not Docker.
   Inspect `ps -eo pid,ppid,user,stat,cgroup,comm` for unexpected runtimes. Do not
   invoke `docker`, `podman`, `ctr`, `nerdctl`, `crictl`, a Docker SDK, `curl` against
   a daemon socket, socket connection probes, `systemctl start`, daemon reload,
   or package installation. Even `docker ps` can activate `docker.socket`.
2. Read installed unit files/drop-ins and daemon configuration as files, without
   sourcing or executing them. Inspect `ExecStart` only locally for `--data-root`,
   `--config-file`, socket paths and alternate services. Record only unit paths,
   data-root paths and restart mechanisms, not entire commands/environments.
   `/etc/docker/daemon.json` may identify `data-root`; default `/var/lib/docker`
   is only a candidate until configuration confirms it. Review root/user cron,
   timers, rc.local, rootless service directories and containerd/Kubernetes/Podman
   launch paths separately. Do not read Docker auth/config credential files.
3. With the daemon confirmed inactive, inspect ONLY the confirmed Docker data
   root's `containers/<id>/hostconfig.json` and `config.v2.json` as static files.
   Do not traverse layers, mounted volumes, application data or container logs.
   An operator-approved local `jq` read can project only these fields:

       jq '{RestartPolicy: .RestartPolicy, AutoRemove: .AutoRemove}' /CONFIRMED_DATA_ROOT/containers/CONFIRMED_ID/hostconfig.json
       jq '{ID: .ID, Name: .Name, State: {Running: .State.Running, Paused: .State.Paused, Restarting: .State.Restarting}}' /CONFIRMED_DATA_ROOT/containers/CONFIRMED_ID/config.v2.json

   Paths above are placeholders, never executable guessed values. Do not install
   jq if absent: use a separately reviewed offline reader or keep containment and
   stop. Do not print complete JSON (`Config.Env`, labels, mounts and commands may
   contain secrets). Offline `State` is historical metadata, NOT current process
   truth. Record each exact ID, restart policy, path, inspection time and parse
   errors. No JSON rewriting. Missing files, unreadable storage, unknown custom
   runtime metadata or unmatched launchers mean inventory INCOMPLETE, not idle.
4. Do not start a daemon to discover information missing from offline files.
   Preserve current/previous journals and queue/assets under the existing
   incident procedure; no cleanup, replay or workload test.

## Exact persistent containment gate before leaving emergency mode

Present the owner the confirmed unit/socket/container IDs, restart policies, unrelated
impact, existing masks/drop-ins and rollback state. Obtain exact authorization for
each confirmed path. If narrow safe containment cannot be verified offline,
request explicit persistent masks of the confirmed Docker service/socket and
containerd unit names, with the ALL-container impact above. For the standard names
ONLY after installation and scope are confirmed, the approved action is:

    systemctl mask docker.service docker.socket containerd.service
    systemctl show docker.service docker.socket containerd.service -p LoadState -p ActiveState -p FragmentPath
    systemctl is-enabled docker.service docker.socket containerd.service

Do not use `--now` or `--force`. Check custom instance units before masking; a
conflict is a STOP, not permission to overwrite. Verify persistent symlinks under
`/etc/systemd/system/`, not just `/run/` masks from the one-time kernel arguments.
If root needs a writable remount, that is a separately specified approval step.
No existing container restart policy is changed by this packet. Custom daemons,
rootless runtimes and non-Docker launchers need exact reviewed containment too.

Remain in emergency mode until ALL discovered load paths have verified persistent
containment and their side effects are approved. Network-only normal boot is a
separate approval. Docker/socket/containerd must remain inactive after that boot;
never start them as a health test. Rollback preserves containment: unmasking or
starting a runtime may immediately restore every eligible container. Restore only
exact approved pre-state after narrower guards are verified. This is a staged
procedure, not demonstrated live rescue or an unattended recovery implementation.
