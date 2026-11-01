# Kubernetes pod in CrashLoopBackOff

service: kubernetes
component: kubelet
severity: high
version_context: Kubernetes 1.28-1.33
published_at: 2026-10-25
last_verified: 2026-10-25

## Symptoms
The pod starts, exits, and is restarted with a growing delay between attempts.

  NAME                     READY   STATUS             RESTARTS   AGE
  api-7d9f8b6c4-x2klm      0/1     CrashLoopBackOff   7          12m

In the pod events:
  Back-off restarting failed container api in pod api-7d9f8b6c4-x2klm

The backoff doubles from 10 seconds and is capped at 5 minutes, so a pod that
has been failing for a while appears to hang between attempts. CrashLoopBackOff
is the kubelet's response to repeated exits, not a cause in itself.

## Impact
The deployment never reaches its desired ready count. During a rolling update
the new revision never becomes ready, so the rollout stalls. If maxUnavailable
allowed old pods to be removed first, capacity is already reduced.

## Diagnosis
Start with the exit code, which narrows the cause more than the logs do:

  kubectl describe pod POD_NAME

Read Last State, Reason and Exit Code. Exit code 1 is an application error.
137 is SIGKILL, usually OOMKilled, which describe will name. 139 is a
segmentation fault. 143 is SIGTERM.

Read the logs from the previous attempt, since the current container may not
have produced any:

  kubectl logs POD_NAME --previous
  kubectl logs POD_NAME --previous --all-containers

Check events for causes that happen before the process runs:

  kubectl get events --sort-by=.lastTimestamp

A missing ConfigMap or Secret, an image pull failure, or a command that is not
executable all appear here rather than in the application log.

If the container exits too fast to inspect, attach a debug container to the
running pod instead of guessing:

  kubectl debug -it POD_NAME --image=busybox:1.36 --target=api

## Remediation
1. Fix according to exit code. For 137, treat it as an OOM kill and raise the
   memory limit. For 1 or 139, fix the fault shown in the previous logs.
2. If a referenced ConfigMap or Secret is missing, create it. The kubelet
   retries automatically once it exists.
3. If a liveness probe is killing a healthy but slow-starting container, add a
   startupProbe so the liveness probe does not begin until startup completes.
4. If the entrypoint is wrong, correct command and args. An exec format error
   means a missing interpreter line or an image built for another architecture.
5. Roll out the fix and confirm restarts stop:
   kubectl rollout restart deployment DEPLOYMENT_NAME
   kubectl get pods -w

## Prevention
Validate manifests in CI so a missing key fails the pipeline rather than the
cluster.

Use a startupProbe for anything with a slow boot, and keep liveness probes
narrow. A liveness probe that checks a downstream dependency converts an
outage elsewhere into a restart loop here.

Alert on pods not ready for longer than a few minutes, which catches this
regardless of the underlying exit code.
