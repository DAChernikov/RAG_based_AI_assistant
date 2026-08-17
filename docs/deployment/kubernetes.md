# Kubernetes deployment

The Helm chart supports external PostgreSQL, Redis and self-hosted model endpoints. It does not install a public database or cloud-specific infrastructure.

```bash
kubectl create namespace rag-assistant
kubectl -n rag-assistant create secret generic rag-assistant-secrets \
  --from-literal=database-url='<postgres-tls-placeholder>' \
  --from-literal=redis-url='<redis-tls-placeholder>' \
  --from-literal=jwt-secret='<generated-placeholder>'
helm lint deploy/helm/rag-assistant
helm template rag deploy/helm/rag-assistant -n rag-assistant -f values.production.yaml >/tmp/rag-rendered.yaml
helm upgrade --install rag deploy/helm/rag-assistant -n rag-assistant -f values.production.yaml --wait
kubectl -n rag-assistant get pods,jobs,ingress
```

Use External Secrets in preference to CLI-created Secrets. Values configure immutable image digests, HTTPS Ingress/cert-manager issuer, exact CORS origin, external TLS endpoints, PVC for embedding cache, probes and resources. The migration Job must reach completion before API/workers start.

The chart creates separate service accounts, default-deny NetworkPolicies, non-root/read-only security contexts, PDB and HPA for stateless services. Restrict egress to DNS, PostgreSQL, Redis, configured source hosts and model endpoints. Bind `/metrics` to monitoring only. Model GPU deployments normally live in a separate node pool/service.

Validate rollout with `/health`, `/ready`, authenticated smoke and tenant-isolation tests. Roll back application manifests with `helm rollback rag <revision>` only when schema compatibility permits; otherwise restore the pre-upgrade PostgreSQL backup. Verify TLS renewal, HPA behavior, disruption budget and NetworkPolicy enforcement in the target cluster.
