# Disaster recovery

This runbook rebuilds the Mac mini DNS and monitoring stack after host loss, disk erase,
or replacement. It is not the first response to an ordinary outage. Use
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) before declaring a rebuild.

Capitalized requirement words use the RFC 8174 meanings defined in
[README.md](README.md#safety-contract).

## When to use this runbook

A full rebuild is justified when one or more of these conditions is confirmed:

- the host or system volume cannot boot or be repaired safely;
- intended configuration cannot be recovered on the existing operating system;
- replacement hardware is taking over `192.168.0.100`;
- the operator explicitly chooses a clean rebuild after preserving required state.

A WAN outage, one broken domain, a stopped service, a bad dashboard, or a failed
blocklist update is not by itself a disaster-recovery event.

## Recovery invariants

- The repository is the source for intended configuration and scripts.
- Runtime-generated files MUST NOT be copied into Git as a substitute for source.
- Secrets and private household data MUST remain outside the repository.
- The generated RPZ zonefile MUST exist before `unbound-checkconf` validates the tracked config.
- Unbound MUST be managed with root `brew services`.
- Prometheus, Grafana, and node_exporter MUST run as system LaunchDaemons and MUST NOT be
  started with `brew services`.
- Every validator MUST pass before the corresponding service is activated.
- Router DHCP and reservations MUST be verified independently; this repository cannot
  restore them.

## Preserve runtime-only state

Perform this only while the old mini is reachable.

| Field | Value |
|---|---|
| Execution location | Protected operator workstation, pulling from the old mini |
| Privilege | SSH as `localadmin`; additional file permissions MAY require controlled elevation on the old host |
| State | Read-only on source host; writes sensitive backup files on workstation |
| Success | Required files exist in an encrypted or access-controlled backup without being printed |
| Failure | Missing file, permission error, incomplete copy, or backup stored inside the repository |

| Item | Need | Sensitivity | Restore consequence if absent |
|---|---|---|---|
| ntfy topic | Required to preserve the existing channel | Secret | Create a new random topic and resubscribe devices |
| Personal warm list | Optional | Household behavior profile | Regenerated after traffic repopulates the cache |
| Grafana database | Optional | Credentials, sessions, local state | File dashboards return; users/passwords reset |
| Prometheus TSDB | Optional | Operational history | Monitoring starts with empty history |
| Root trust anchor | Optional | Managed state | Unbound recreates/updates it when correctly configured |

The backup process MUST NOT print the ntfy topic, include it in shell tracing, or place it
under the repository root. Restrict backup directory permissions before copying.

```sh
# Operator workstation, outside the repository
install -d -m 700 /secure/path/mini-recovery
scp localadmin@192.168.0.100:/Users/localadmin/monitoring/ntfy.topic \
  /secure/path/mini-recovery/
chmod 600 /secure/path/mini-recovery/ntfy.topic
```

Copy optional state only when its sensitivity and size are acceptable. A repository
checkout and the runtime-state backup MUST be stored separately.

## Prerequisites

| Dependency | Requirement |
|---|---|
| macOS | Supported Apple Silicon installation |
| Account | Local administrator named `localadmin` |
| Network | Ethernet address reserved as `192.168.0.100` after cutover |
| Toolchain | Xcode command-line tools, Homebrew under `/opt/homebrew`, Git, Go |
| Packages | `unbound`, `prometheus`, `grafana`, `node_exporter`, `go` |
| External binaries | `unbound_exporter`, `blackbox_exporter` under `/usr/local/sbin` |
| Repository | Reviewed checkout on the mini or a workstation that can deploy to it |

Exporter versions are not pinned by this repository. A rebuild therefore MUST record
the installed versions and validate their flags and metrics against the tracked configs.

## Recovery sequence

### 1. Install the base system

| Field | Value |
|---|---|
| Execution location | Replacement mini console |
| Privilege | Local administrator; installation commands change system state |
| Success | `localadmin` can use SSH, Homebrew reports `/opt/homebrew`, and required packages are installed |
| Failure | Wrong architecture prefix, missing command-line tools, or SSH inaccessible |

Install macOS, create `localadmin`, enable Remote Login temporarily under the local
security policy, install Xcode command-line tools, and install Homebrew. Then install:

```sh
brew install unbound prometheus grafana node_exporter go
```

Build the two external exporters from their documented modules and install the binaries
root-owned under `/usr/local/sbin`:

```sh
go install github.com/letsencrypt/unbound_exporter@latest
go install github.com/prometheus/blackbox_exporter@latest
```

Use `go env GOPATH` to locate outputs. Because `@latest` is not reproducible, the
operator SHOULD record module versions before continuing.

### 2. Obtain the repository

Clone or restore a reviewed checkout. Record the commit and `git status --short`. A
dirty checkout MAY be used only when the exact diff is intentional and retained in the
recovery record.

Run repository validation before deployment:

```sh
tools/validate-repository.sh
```

### 3. Create runtime directories

| Field | Value |
|---|---|
| Execution location | Mini |
| Privilege/state | `sudo`; creates runtime directories and ownership |
| Success | Directories exist with intended owners; `localadmin` can write the textfile directory through group `admin` |
| Failure | Wrong owner/mode or non-Homebrew prefix |

```sh
sudo install -d -o unbound -g unbound -m 0755 /opt/homebrew/etc/unbound/blocklists
sudo install -d -o localadmin -g admin -m 0755 /opt/homebrew/etc/grafana/dashboards
sudo install -d -o localadmin -g admin -m 0755 /opt/homebrew/etc/grafana/provisioning/dashboards
sudo install -d -o localadmin -g admin -m 0755 /opt/homebrew/etc/grafana/provisioning/datasources
sudo install -d -o root -g admin -m 0775 /opt/homebrew/var/lib/node_exporter/textfile
sudo install -d -o localadmin -g staff -m 0700 /Users/localadmin/monitoring/state
sudo install -d -o localadmin -g staff -m 0755 /Users/localadmin/warmup
sudo install -d -o root -g wheel -m 0755 /usr/local/sbin
sudo install -d -o root -g wheel -m 0755 /usr/local/libexec/unbound-stack/dns
sudo install -d -o root -g wheel -m 0755 /usr/local/libexec/unbound-stack/monitoring
sudo install -d -o root -g wheel -m 0755 /usr/local/share/unbound-stack
```

The group-writable textfile directory is an explicit trust boundary; see
[ARCHITECTURE.md](ARCHITECTURE.md#ownership-and-privilege-boundaries).

### 4. Install Unbound files and generate RPZ zones

| Field | Value |
|---|---|
| Execution location | Mini from repository checkout |
| Privilege/state | `sudo`; installs resolver policy and downloads blocklists |
| Success | Both generated zones exist, `unbound-checkconf` passes, exactly one root-managed service starts |
| Failure | Download floor failure, invalid zone/config, duplicate process, or missing control socket after start |

Install the tracked Unbound files:

```sh
sudo install -o unbound -g unbound -m 0644 config/unbound/unbound.conf /opt/homebrew/etc/unbound/unbound.conf
sudo install -o unbound -g unbound -m 0644 config/unbound/root.hints /opt/homebrew/etc/unbound/root.hints
sudo install -o unbound -g unbound -m 0644 config/unbound/blocklists/hagezi-allow.rpz /opt/homebrew/etc/unbound/blocklists/hagezi-allow.rpz
sudo install -o root -g wheel -m 0755 scripts/dns/update-blocklist.sh /usr/local/libexec/unbound-stack/dns/update-blocklist.sh
sudo install -o root -g wheel -m 0755 scripts/dns/manage-blocklist.sh /usr/local/libexec/unbound-stack/dns/manage-blocklist.sh
sudo install -o root -g wheel -m 0644 config/unbound/blocklist-source /opt/homebrew/etc/unbound/blocklist-source
```

Run the updater before config validation:

```sh
sudo /usr/local/libexec/unbound-stack/dns/update-blocklist.sh
```

Before Unbound starts, the updater may install a valid zone and then return failure only
because the control socket does not exist. The operator MUST distinguish that expected
reload failure from download or validation failure and MUST verify the zonefile before
continuing.

```sh
test -s /opt/homebrew/etc/unbound/blocklists/hagezi-normal.rpz
/opt/homebrew/opt/unbound/sbin/unbound-checkconf /opt/homebrew/etc/unbound/unbound.conf
sudo brew services start unbound
```

The non-root form `brew services start unbound` MUST NOT be used.

### 5. Install monitoring configuration

| Field | Value |
|---|---|
| Execution location | Mini from repository checkout |
| Privilege/state | `sudo` for system config ownership; installs monitoring files but does not start services yet |
| Success | Files match the manifest mapping and all available staged/live validators pass |
| Failure | Wrong path/owner, JSON/YAML error, failed self-test, or missing external binary |

Install Prometheus, blackbox, node_exporter, and Grafana sources using the exact mapping
in [REPO_LAYOUT.md](REPO_LAYOUT.md#source-to-runtime-summary). Runtime config files and
Grafana directories SHOULD be owned by `localadmin:admin` so their LaunchDaemons and
future reviewed deployments use the same model.

Validate before starting:

```sh
/opt/homebrew/opt/prometheus/bin/promtool check config /opt/homebrew/etc/prometheus.yml
/opt/homebrew/opt/prometheus/bin/promtool check rules /opt/homebrew/etc/prometheus.rules.yml
test "$(find /opt/homebrew/etc/grafana/dashboards -type f -name '*.json' | wc -l | tr -d ' ')" = 5
for file in /opt/homebrew/etc/grafana/dashboards/*.json; do
  python3 -m json.tool "$file" >/dev/null || exit 1
done
grep -q 'allowUiUpdates: false' \
  /opt/homebrew/etc/grafana/provisioning/dashboards/unbound.yml
grep -q 'path: /opt/homebrew/etc/grafana/dashboards' \
  /opt/homebrew/etc/grafana/provisioning/dashboards/unbound.yml
```

Install all scheduled custom code under `/usr/local/libexec/unbound-stack`, owned by
`root:wheel` and mode 0755. Install the curated warm list under
`/usr/local/share/unbound-stack`; keep only sensitive mutable state under
`/Users/localadmin`. Operator helpers belong under `/usr/local/sbin`.

Run non-network self-tests:

```sh
/usr/bin/python3 /usr/local/libexec/unbound-stack/monitoring/apple_content_cache_exporter.py --test
/usr/bin/python3 /usr/local/libexec/unbound-stack/monitoring/unbound_client_rpz_exporter.py --test
/usr/bin/python3 /usr/local/libexec/unbound-stack/monitoring/time_machine_exporter.py --test
/usr/bin/python3 /usr/local/libexec/unbound-stack/monitoring/prometheus_alert_notifier.py --selftest
```

### 6. Install LaunchDaemons

| Field | Value |
|---|---|
| Execution location | Mini |
| Privilege/state | `sudo`; installs and starts system jobs |
| Success | Every plist passes validation; KeepAlive services run; periodic jobs complete with exit 0 |
| Failure | plist error, missing program/config, wrong owner/mode, or repeated crash loop |

Validate every plist before installing it:

```sh
plutil -lint launchd/*.plist
```

Install only `local.unbound.*.plist` files into `/Library/LaunchDaemons`, owned
`root:wheel` mode 0644. The `homebrew.mxcl.unbound.plist` snapshot MUST NOT be copied;
root `brew services` owns that definition.

Bootstrap each system job only after its program, config, directories, and log path are
ready. Required labels and identities are listed in
[REFERENCE.md](REFERENCE.md#services-and-scheduled-jobs).

### 7. Restore notification state

| Field | Value |
|---|---|
| Execution location | Mini using a protected backup source |
| Privilege/state | Controlled file restore; changes secret and notifier state without printing it |
| Success | Topic exists as `localadmin:staff` mode 0600 and notifier self-test passes |
| Failure | Missing/invalid topic, unsafe permissions, self-test error, or accidental disclosure |

Restore the ntfy topic from protected backup without printing it, set owner
`localadmin:staff` and mode 0600, ensure the notifier state directory is mode 0700, then
run notifier `--selftest`. If the old topic is unavailable, generate a new random topic
locally and resubscribe the operator's device.

`--test-send` MAY be run only when one real notification is intended. It redacts the
topic in output. The topic MUST NOT be embedded in a command line or diagnostic paste.

### 8. Restore platform services and security

| Field | Value |
|---|---|
| Execution location | Mini console and System Settings; second workstation terminal for SSH proof |
| Privilege/state | `sudo` and administrator UI; changes platform, firewall, and access state |
| Success | Intended services are enabled, firewall permits only required listeners, fresh key-only SSH succeeds, Screen Sharing is off |
| Failure | Lockout risk, unexpected listener, failed firewall reachability, or platform service unavailable |

- Enable Apple Content Caching in macOS System Settings if the host is intended to run
  it. The repository only monitors the feature.
- Install the Tailscale app/System Extension if remote access is required. Do not create
  a Homebrew Tailscale service.
- Deploy the sshd fragment only after `sudo sshd -t` succeeds. Keep the original session
  open and prove a new key-only login before closing it.
- Install `screenshare`; leave Screen Sharing disabled.
- Install the tracked newsyslog file and validate with `sudo newsyslog -nv -f`.
- Authorize the resolved Cellar paths for Unbound, Prometheus, and Grafana in the macOS
  application firewall. Re-test LAN reachability.

These are state-changing platform operations. A failure MUST be corrected before client
cutover.

## Acceptance checklist

Complete the checks in order. Initial breadcrumb alerts MAY remain until each periodic
job has succeeded once; force each job individually and verify its actual output.

- [ ] `unbound-checkconf` passes on the live config.
- [ ] Exactly one Unbound process listens on port 53.
- [ ] `cloudflare.com` returns `NOERROR` with `ad`.
- [ ] `google-analytics.com` returns authoritative `NXDOMAIN`.
- [ ] `dnssec-failed.org` returns `SERVFAIL`.
- [ ] `mini.lan` returns `192.168.0.100`.
- [ ] Prometheus readiness succeeds and all eight target instances are healthy.
- [ ] `node_textfile_scrape_error` is zero.
- [ ] All five dashboards exist and render through Grafana's LAN address.
- [ ] Grafana `/api/health` reports database `ok`.
- [ ] Every scheduled producer has seeded its expected textfile/breadcrumb; Time Machine
      is explicitly configured, disabled, or failed rather than inferred from a mounted volume.
- [ ] The ntfy test notification arrives if phone alerting is required.
- [ ] A fresh SSH session succeeds with key auth and password auth is rejected.
- [ ] Screen Sharing is closed after any recovery use.
- [ ] `python3 tools/verify-deployment.py` reports `RESULT: CLEAN`.

An `INCOMPLETE` drift report is not acceptance.

## Client cutover

| Field | Value |
|---|---|
| Execution location | Router administration and representative LAN clients |
| Privilege/state | External state-changing network configuration |
| Success | Clients receive `192.168.0.100` as primary DNS and can resolve public, RPZ, and `.lan` canaries |
| Failure | Clients use only external DNS, lose `.lan`, bypass RPZ, or cannot resolve |

Only after acceptance:

1. assign or reserve `192.168.0.100` for the rebuilt mini;
2. verify router DHCP advertises the intended resolver order;
3. renew representative clients' network leases;
4. test public DNS, the RPZ canary, and `.lan` from a client;
5. watch alerts and query/cache behavior during real traffic.

The documented `9.9.9.9` fallback MUST be verified on the router. It preserves public
resolution during a mini outage but bypasses local policy.

## Rollback

If acceptance fails, do not leave a partially working resolver as primary DNS.

- Restore the previous router DNS configuration or temporarily use an approved public
  resolver while diagnosis continues.
- For one bad tracked change, deploy the last-known-good version of that file, run its
  validator, and reload only its owner.
- Generated blocklist updates normally retain a backup and roll back validation
  failures.
- Dashboard rollback is a validated JSON redeployment, not a UI edit.
- A failed sshd change MUST be corrected from the still-open original session or local
  console.

Potentially destructive disk erasure, database replacement, and router cutover require
an operator-confirmed recovery plan and are outside repository automation.

## Recovery record

Record the following without secret values:

- hardware and macOS version;
- repository commit and any intentional diff;
- Homebrew package versions and Go module versions;
- runtime-state items restored or intentionally omitted;
- validators and acceptance checks with results;
- firewall and router changes;
- new Tailscale identity or ntfy subscription work;
- remaining gaps and rollback state.
