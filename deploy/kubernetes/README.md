# Omnigent on Kubernetes

Deploy Omnigent to any Kubernetes cluster using Kustomize. The manifests pull
the prebuilt image and wire up a persistent volume, health checks, and Ingress
with TLS.

## What gets provisioned

- **Deployment** — single-replica pod running
  `ghcr.io/omnigent-ai/omnigent-server`, served on port 8000.
- **Service** — ClusterIP on port 80 → 8000.
- **Ingress** — HTTPS via cert-manager (nginx ingress class by default).
- **PVC** — 10 Gi volume at `/data/artifacts` for the artifact store, minted
  cookie secret, and admin credentials.
- **ConfigMap + Secret** — environment config and database credentials.

## Prerequisites

- A Kubernetes cluster (1.25+)
- `kubectl` with Kustomize support (`kubectl kustomize` or standalone `kustomize`)
- An Ingress controller (e.g. ingress-nginx) and cert-manager for TLS
- A PostgreSQL database (managed or in-cluster — see below)

## Deploy with an external database

Use this path when you have a managed Postgres (RDS, Cloud SQL, Neon, etc.).

1. **Edit the secret** — set your real `DATABASE_URL` and generate a cookie
   secret:

   ```bash
   # deploy/kubernetes/base/secret.yaml
   DATABASE_URL: "postgresql+psycopg://user:pass@your-db-host:5432/omnigent"
   OMNIGENT_ACCOUNTS_COOKIE_SECRET: "$(openssl rand -hex 32)"
   ```

2. **Edit the Ingress hostname** — replace `omnigent.example.com` in
   `base/ingress.yaml` with your actual domain.

3. **Apply:**

   ```bash
   kubectl kustomize deploy/kubernetes/base/ | kubectl apply -f -
   ```

4. **Admin password** prints once in the first-boot logs:

   ```bash
   kubectl logs -n omnigent deploy/omnigent | grep "password:"
   ```

   Also written to `/data/admin-credentials` on the volume.

## Deploy with in-cluster Postgres

The `overlays/postgres/` overlay adds a single-replica Postgres 16 StatefulSet
with its own 10 Gi PVC. Good for dev/testing clusters.

1. **Edit secrets** — in `overlays/postgres/secret-patch.yaml`, replace
   `changeme` with real passwords:

   ```bash
   POSTGRES_PASSWORD: "<strong-password>"
   DATABASE_URL: "postgresql+psycopg://omnigent:<strong-password>@postgres:5432/omnigent"
   OMNIGENT_ACCOUNTS_COOKIE_SECRET: "$(openssl rand -hex 32)"
   ```

2. **Edit the Ingress hostname** in `base/ingress.yaml`.

3. **Apply:**

   ```bash
   kubectl kustomize deploy/kubernetes/overlays/postgres/ | kubectl apply -f -
   ```

## Use your own IdP instead (OIDC)

Add OIDC env vars to the secret:

```bash
kubectl create secret generic omnigent-oidc -n omnigent \
  --from-literal=OMNIGENT_AUTH_PROVIDER=oidc \
  --from-literal=OMNIGENT_OIDC_ISSUER=https://github.com \
  --from-literal=OMNIGENT_OIDC_CLIENT_ID=<client-id> \
  --from-literal=OMNIGENT_OIDC_CLIENT_SECRET=<client-secret> \
  --from-literal=OMNIGENT_OIDC_REDIRECT_URI=https://omnigent.example.com/auth/callback \
  --from-literal=OMNIGENT_OIDC_COOKIE_SECRET=$(openssl rand -hex 32)
```

Then add `envFrom: [{secretRef: {name: omnigent-oidc}}]` to the Deployment
container spec (or merge the values into `omnigent-secrets`).

## Resource sizing

The server idles around ~275 MB RSS. The manifests request 512 Mi and limit at
1 Gi — adjust to taste. The first boot against a remote Postgres runs
migrations and takes ~1 minute; bump the liveness `initialDelaySeconds` to
~90s if you see the pod get killed during the first deploy.

## Scaling

The server uses an in-memory runner registry, so **only one replica is
supported**. Do not increase `replicas` unless the architecture is changed to
use a shared registry (e.g. Redis).
