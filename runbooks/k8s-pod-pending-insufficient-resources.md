# Kubernetes pod pending on insufficient resources

service: kubernetes
component: scheduler
severity: medium
version_context: Kubernetes 1.28-1.33 with cluster-autoscaler
published_at: 2026-10-25
last_verified: 2026-10-25

## Symptoms
The pod is accepted by the API server but never assigned to a node.

  NAME                     READY   STATUS    RESTARTS   AGE
  worker-5c8d47f9b-qn4tz   0/1     Pending   0          9m

In the pod events:
  Warning  FailedScheduling  0/5 nodes are available: 5 Insufficient cpu.
  Warning  FailedScheduling  0/5 nodes are available: 3 Insufficient memory, 2 Insufficient cpu.

The message counts nodes by reason, and the counts sum to the cluster size.
That is how to tell this apart from an affinity or taint failure, which names
labels and taints instead of resources.

## Impact
The workload does not run at all. During a scale-up the new replicas never
arrive, so load stays concentrated on existing pods. If this happens during a
rolling update with surge enabled, the update stalls with the old revision
still serving.

## Diagnosis
Read the exact scheduler message, which names the constrained resource:

  kubectl describe pod POD_NAME
  kubectl get events --field-selector reason=FailedScheduling --sort-by=.lastTimestamp

Compare the pod's requests against what nodes have free. Requests, not limits,
are what the scheduler uses:

  kubectl get pod POD_NAME -o jsonpath='{.spec.containers[*].resources.requests}'
  kubectl describe node NODE_NAME

The Allocated resources block at the end of describe node shows requests
already committed as a percentage of allocatable. A node can be fully reserved
while kubectl top shows it nearly idle, because requests are reservations
rather than measurements.

Rule out an admission limit rather than a capacity limit:

  kubectl get resourcequota -n NAMESPACE
  kubectl get limitrange -n NAMESPACE

A quota rejection appears on the ReplicaSet as a FailedCreate event, so check
there if no Pending pod exists at all.

## Remediation
1. Confirm the request is realistic. A request set far above measured usage is
   the most common cause, and lowering it fixes the problem without new nodes:
   kubectl top pod -l app=worker --containers
2. Lower resources.requests.cpu or memory to just above observed steady usage,
   keeping limits where they are.
3. If the request is correct and the cluster is genuinely full, add capacity.
   With cluster-autoscaler, confirm it saw the pod:
   kubectl -n kube-system logs deployment/cluster-autoscaler --tail=100
4. If the autoscaler declined, read the reason. A pod requesting more than any
   single node type provides will never trigger a scale-up, and needs either a
   larger node group or a smaller request.
5. If a ResourceQuota is the binding limit, raise the quota or free it by
   removing unused workloads in the namespace.

## Prevention
Set requests from measured usage, reviewed periodically. Requests drift away
from reality faster than limits do, because nothing fails when they are wrong.

Keep spare capacity so a scale-up does not wait on a node boot. Low-priority
placeholder pods reserve room that real workloads preempt immediately.

Alert on any pod Pending for more than five minutes. The condition is silent
otherwise, since nothing is crashing.
