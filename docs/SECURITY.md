# Access security

Caddy is the only service publishing ports. It requires the same HTTP Basic login on every app, including localhost, and forwards requests over the private Compose network. Use Tailscale for remote access; do not forward router ports to this host.

Run `make auth-password` to set or change the shared username and password, then `make proxy` to recreate the proxy with the new credentials. The password is stored only as a bcrypt hash. The hash is single-quoted in `.env`, so Compose preserves its dollar signs literally. Keep `.env` private and never commit it. `AITD_PASSWORD` can supply the password to `scripts/check-app.py --live`.

The proxy uses plain HTTP. Basic authentication can be sniffed by someone on the LAN; Tailscale encrypts traffic between tailnet devices. A future improvement is HTTPS with `tailscale cert`.
