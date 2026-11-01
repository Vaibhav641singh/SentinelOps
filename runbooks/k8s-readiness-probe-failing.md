# Kubernetes readiness probe failing

service: kubernetes
component: kubelet
severity: high
version_context: Kubernetes 1.28-1.33
published_at: 2026-10-25
last_verified: 2026-10-25

## Symptoms
The pod is Running but never becomes Ready, and it is not restarted.

  NAME                     READY   STATUS    RESTARTS   AGE
  api-6f9c7b54d-vm8pq      0/1     Running   0          6m

In the pod events:
  Warning  Unhealthy  Readiness probe failed: HTTP probe failed with statuscode: 503
  Warning  Unhealthy  Readiness probe failed: Get "http://10.244.3.17:8080/ready": context deadline exceeded

Downstream, the Service has no endpoints and callers receive 503 from the
ingress. The absence of restarts is the signal that separates this from a
liveness failure: a failed readiness probe removes the pod from the Service,
it does not kill the container.

## Impact
Capacity silently disappears. If every replica fails readiness the Service has
no endpoints at all and the application is fully down, while every pod reports
Running and no container has crashed. During a rolling update the new revision
never becomes ready, so the rollout blocks.

## Diagnosis
Read the probe result and the probe configuration together:

  kubectl describe pod POD_NAME
  kubectl get pod POD_NAME -o jsonpath='{.spec.containers[*].readinessProbe}'

Confirm the Service lost its endpoints, which is the user-visible effect:

  kubectl get endpointslice -l kubernetes.io/service-name=SERVICE_NAME
  kubectl get endpoints SERVICE_NAME

Call the endpoint the way the kubelet does, from inside the pod:

  kubectl exec POD_NAME -- curl -sv -m 2 http://localhost:8080/ready

Three outcomes, three different causes. A 200 here means the probe port or path
in the manifest is wrong. A 503 means the application is deliberately reporting
itself not ready, and its logs will say why. A hang means the handler is slower
than timeoutSeconds.

  kubectl logs POD_NAME --tail=100

## Remediation
1. If the probe returns 503 by design, fix the dependency the application is
   waiting on. The probe is reporting correctly and should not be changed.
2. If the path or port does not match the application, correct httpGet.path and
   httpGet.port in the readinessProbe.
3. If the handler is slow under load, raise timeoutSeconds above the observed
   response time and raise failureThreshold so a single slow response does not
   remove the pod from rotation.
4. If the application is simply slow to boot, add a startupProbe rather than
   inflating initialDelaySeconds. The readiness probe then begins only once
   startup has succeeded.
5. Apply and confirm endpoints return:
   kubectl rollout restart deployment DEPLOYMENT_NAME
   kubectl get endpointslice -l kubernetes.io/service-name=SERVICE_NAME -w

## Prevention
Keep readiness and liveness semantically separate. Readiness answers whether
this pod should receive traffic now; liveness answers whether it must be
restarted. Pointing both at the same handler turns a transient dependency blip
into a restart loop.

Do not check downstream dependencies in a readiness probe shared by every
replica. If the database is briefly unavailable, every pod fails readiness at
once and the Service empties, converting a degraded dependency into a total
outage.

Alert on ready replicas falling below desired replicas, which catches this even
though nothing is crashing and no container has a non-zero restart count.
