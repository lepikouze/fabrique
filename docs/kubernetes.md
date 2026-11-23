# Installing Fabrique on Kubernetes

The Helm chart [`deploy/helm/fabrique`](../deploy/helm/fabrique) installs the dashboard, the built-in runner, the
launcher and the egress proxy in a namespace. Each job runs in a disposable Pod.

Validated end to end on k3s 1.37 with Helm 4 (one node, work volume on NFS, namespace at the `restricted` level). It
also runs on RKE2; other distributions have not been tried yet.

## What you need

- A recent Kubernetes cluster and Helm.
- Nodes whose CPU exposes SSE4.2 (x86-64-v2 level). On a virtual machine, avoid generic CPU types (`kvm64`, `qemu64`):
  the agent does not start, with the message "this CPU is missing SSE4.2".
- Storage for two volumes: the dashboard's data (`ReadWriteOnce`) and the jobs' work space. This second volume is
  mounted by the runner **and** by the job Pods: on a multi-node cluster it must be `ReadWriteMany` (NFS, CephFS,
  Longhorn RWX…). On a single node, `ReadWriteOnce` is enough.
- A network plugin that enforces NetworkPolicy (Calico, Cilium, Canal, the one in k3s…). Without it, job Pods are not
  confined.
- An Ingress controller to expose the dashboard.

## Installing

```sh
git clone https://github.com/lepikouze/fabrique.git && cd fabrique
kubectl create namespace fabrique
helm install fabrique deploy/helm/fabrique -n fabrique -f my-values.yaml
```

A minimal values file for production:

```yaml
ingress:
  enabled: true
  className: nginx                 # or traefik…
  host: fabrique.example.org
runner:
  persistence:
    accessMode: ReadWriteMany      # required as soon as there are several nodes
    storageClass: nfs-client       # a storage class that supports ReadWriteMany
image:
  tag: latest                      # pin a specific version
```

Helm then prints what to do next: read the setup code in the dashboard's logs, open the address, create the
administrator account, then follow the Setup page. The built-in runner registers itself.

Every value is commented in [`values.yaml`](../deploy/helm/fabrique/values.yaml). For a presentation or a trial,
`dashboard.demo: true` turns on demonstration mode (fictitious jobs, runners and costs, shown or hidden in Settings →
Demo); leave it at `false` in real use.

## Secrets

On the first `helm install`, the chart creates a Secret named after the release, with four generated values: the
session secret, the key that encrypts model keys and Git tokens, the built-in runner's token and the launcher's. It
reads them back on every upgrade and never changes them.

- **Back up this Secret.** Without the encryption key, the secrets stored in the dashboard are unreadable.
- Neither it nor the data volume is removed by `helm uninstall`.
- To manage these values yourself (Sealed Secrets, External Secrets, Vault…), create a Secret with the keys
  `session-secret`, `secret-key`, `runner-token` ("fab_" followed by at least 32 characters) and `launcher-token` (at
  least 32 characters), and give its name in `secrets.existingSecret`.
- If you renew the built-in runner's token in the dashboard, put the new one in the Secret then restart the runner:
  the chart never puts an old token back in service.

## What Fabrique can do in the cluster

A single component talks to the Kubernetes API: the launcher. Its service account is limited to the release's
namespace and can only create, read and delete Pods, run a command in them, and read their logs (to extract each
job's network egress from the proxy's log). It has no access to Secrets, Deployments or other namespaces.

Each job Pod is created with a fixed policy that nothing in a request can change:

| Rule | Value |
|---|---|
| Image | one of the allowlisted images (`jobs.images`) |
| User | 10002, with no capability and no privilege escalation |
| File system | read-only; an ephemeral `/tmp` |
| Volume | **this** job's subfolder in the work volume, nothing else |
| Kubernetes API | no token mounted |
| Network | no ingress; egress only to the proxy, the runner's relay and gateway, and DNS |
| Resources | `jobs.memory` and `jobs.cpu` |

The egress proxy only lets through the host names in `proxy.allow` (package registries, by default). So a job reaches
neither the Internet directly, nor your Git host, nor the dashboard. Model keys stay in the runner: the job calls its
gateway, which adds the key.

All of Fabrique's Pods, jobs included, comply with the **`restricted`** level of the Pod Security Standards:

```sh
kubectl label namespace fabrique pod-security.kubernetes.io/enforce=restricted
```

## The work volume on NFS

The runner and the job Pods run as the same user (10002) and do not need root on the volume: an NFS export with
`root_squash` (the default setting) is fine. The exported folder only has to be writable by user 10002.

## Behind a corporate proxy

If the cluster only reaches the Internet through a proxy:

```yaml
outboundProxy:
  url: http://proxy.example.org:3128
  noProxy: .example.org,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
```

The dashboard and the runner use it for whatever is outside the network (hosted Git host, a model provider's API);
the addresses in `noProxy` and Fabrique's own services are still reached directly. The jobs' egress proxy forwards the
requests on its allowlist to it. Give the proxy's address as an IP address or a full name (a short cluster Service
name is not resolved). A proxy that requires credentials is not supported.

## Known limits

- **Number of processes per Pod**: Kubernetes does not limit it in a Pod's manifest; it is a node setting (the
  kubelet's `podPidsLimit`). Set it on the nodes that host jobs.
- **A single dashboard instance**: its database is a SQLite file on a `ReadWriteOnce` volume. Each page makes about
  twenty queries: the storage behind this volume matters. Measured on two different NFS servers: 0.07 ms per query on
  one, 4.85 ms on the other, that is a responsive page or a page above 300 ms. Measure before choosing:

  ```sh
  kubectl exec deploy/<release>-dashboard -- python -c "import sqlite3,time; c=sqlite3.connect('/data/fabrique.db'); t=time.time(); [c.execute('select count(*) from jobs').fetchone() for _ in range(200)]; print((time.time()-t)/200*1000, 'ms')"
  ```
- **Forgotten Pods**: if the runner stops in the middle of a job, the launcher deletes the orphaned Pod after 24 hours
  (and at once if it has finished).
- **Private images**: give the name of the registry secret in `imagePullSecrets`; it is also applied to job Pods.

## Without a Pod per job

`runner.executor: local` runs jobs inside the runner's Pod, separated by a plain Unix user. This is useful for a trial
on a cluster with no NetworkPolicy or shared volume, but it is not a security boundary, and the runner then runs as
root with three capabilities (`baseline` level).
