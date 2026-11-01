# Kubernetes pod OOMKilled

service: kubernetes
component: kubelet
severity: high
version_context: Kubernetes 1.28-1.33, cgroup v2 nodes
published_at: 2026-10-25
last_verified: 2026-10-25

## Symptoms
The container is killed and restarted without the application logging an error,
because it is terminated by the kernel rather than exiting on its own.

In kubectl describe pod:
    Last State:     Terminated
      Reason:       OOMKilled
      Exit Code:    137

On the node, in dmesg or the kernel log:
Memory cgroup out of memory: Killed process 4821 (java) total-vm:...

Metrics: container_memory_working_set_bytes rising to meet the configured limit
just before each restart, and
kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} going to 1.
Restart count climbs steadily rather than all at once.

## Impact
Requests in flight at the moment of the kill are dropped with no response. If
several replicas grow at a similar rate they are killed at similar times, so
capacity drops sharply instead of gradually. Repeated kills push the pod into
CrashLoopBackOff, at which point restarts are delayed and the outage lengthens.

## Diagnosis
Confirm the reason rather than inferring it from restarts:

  kubectl describe pod POD_NAME
  kubectl get pod POD_NAME -o jsonpath='{.status.containerStatuses[*].lastState.terminated.reason}'

Read the logs from the killed instance, not the new one:

  kubectl logs POD_NAME --previous

Compare actual usage against the configured limit:

  kubectl top pod POD_NAME --containers
  kubectl get pod POD_NAME -o jsonpath='{.spec.containers[*].resources}'

A working set that climbs steadily and never falls indicates a leak. A working
set that is flat but above the limit indicates the limit is simply too low.

## Remediation
1. Decide which of the two it is from the usage curve before changing anything.
   Raising the limit on a leak buys hours and hides the cause.
2. If the limit is too low, raise resources.limits.memory with headroom above
   observed peak, and set resources.requests.memory to the same value so the
   pod gets Guaranteed QoS and is evicted last under node pressure.
3. For a JVM workload, set -XX:MaxRAMPercentage=75 so the heap is sized from
   the container limit rather than from node memory.
4. For a Go workload, set GOMEMLIMIT slightly below the container limit so the
   garbage collector runs before the kernel intervenes.
5. Apply and watch restart count stop climbing:
   kubectl rollout restart deployment DEPLOYMENT_NAME
   kubectl get pods -w

## Prevention
Alert on the OOMKilled termination reason directly rather than on restart
count, which cannot distinguish a kill from a crash.

Run the Vertical Pod Autoscaler in recommendation mode to size requests and
limits from observed usage instead of from estimates.

Keep requests equal to limits for memory on latency-sensitive workloads. Memory
is incompressible, so overcommitting it trades a predictable throttle for an
unpredictable kill.
