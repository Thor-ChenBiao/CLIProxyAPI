# TLS certificate automation

`token.zasdas.com` terminates TLS on each CLIProxyAPI node behind a TCP NLB.
Node A is the only ACME issuer. HTTP-01 traffic is routed through a dedicated
NLB TCP port 80 listener and target group that contains only node A.

The renewal flow is:

1. `acme.sh` uses webroot `/var/lib/acme-challenge` and writes renewed material
   to `/var/lib/cliproxy-certificate`.
2. `cliproxy-deploy-token-cert.sh` securely copies the staged files to node B.
3. Node B validates the hostname, validity window, and key pair, then backs up,
   installs, tests Nginx, reloads it, and verifies the certificate on port 8443.
4. The same validated deployment runs on node A.
5. The daily timer also runs the deploy step when no renewal is due, repairing
   certificate drift between nodes.

If node B is unreachable, node A is still updated so the certificate authority
node cannot expire behind a failed worker. The service exits nonzero after the
local update, making the node B repair visible and retrying it on the next run.

The NLB port 443 listener is not changed by this automation.

## Required AWS resources

- NLB TCP port 80 listener.
- TCP port 80 target group with HTTP `/acme-healthz` health checks.
- Only node A registered in that target group.
- Target group attributes:
  - `preserve_client_ip.enabled=false`
  - `load_balancing.cross_zone.enabled=true`
- Node A security group allows TCP port 80 from the VPC CIDR only.

## Initial acme.sh configuration

After the HTTP challenge path is publicly reachable, configure the existing
account and certificate state:

```bash
sudo /root/.acme.sh/acme.sh --issue \
  --home /.acme.sh \
  --server letsencrypt \
  --webroot /var/lib/acme-challenge \
  --keylength ec-256 \
  --domain token.zasdas.com \
  --force

sudo install -d -o root -g root -m 0700 /var/lib/cliproxy-certificate
sudo /root/.acme.sh/acme.sh --install-cert \
  --home /.acme.sh \
  --ecc \
  --domain token.zasdas.com \
  --fullchain-file /var/lib/cliproxy-certificate/fullchain.pem \
  --key-file /var/lib/cliproxy-certificate/key.pem \
  --reloadcmd /usr/local/sbin/cliproxy-deploy-token-cert.sh
```

The `--install-cert` destinations must remain staging paths. Do not point them
directly at Nginx, because cluster deployment must update node B before node A.

## Rollback

Each node retains the five newest `backup-*` directories under
`/etc/nginx/ssl/token.zasdas.com`. To roll back a node, install the selected
backup as `fullchain.pem` and `key.pem`, run `nginx -t`, and reload Nginx.

To disable automatic renewal without touching the active certificate:

```bash
sudo systemctl disable --now cliproxy-cert-renew.timer
```
