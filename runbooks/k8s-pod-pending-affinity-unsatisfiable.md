# Kubernetes pod pending on unsatisfiable affinity

service: kubernetes
component: scheduler
severity: medium
version_context: Kubernetes 1.28-1.33
published_at: 2026-10-25
last_verified: 2026-10-25

## Symptoms
The pod stays Pending, and the scheduler names labels, taints or topology
rather than resources.

  Warning  FailedScheduling  0/6 nodes are available: 3 node(s) didn't match Pod's node affinity/selector, 3 node(s) had untolerated taint {dedicated: gpu}.
  Warning  FailedScheduling  0/6 nodes are available: 6 node(s) didn't match pod anti-affinity rules.
  Warning  FailedScheduling  0/6 nodes are available: 2 node(s) didn't match pod topology spread constraints.

This is the distinguishing feature against an insufficient-resources failure.
Adding nodes of the wrong shape will not help, and often makes the message
longer without changing the outcome.

## Impact
The workload does not start. Anti-affinity failures are the most misleading:
the cluster has room, but the rule forbids placing another replica where room
exists, so scaling the deployment produces permanently Pending replicas.

## Diagnosis
Read the full message, which counts nodes per reason:

  kubectl describe pod POD_NAME

Compare what the pod requires against what nodes offer:

  kubectl get pod POD_NAME -o jsonpath='{.spec.affinity}'
  kubectl get pod POD_NAME -o jsonpath='{.spec.nodeSelector}'
  kubectl get nodes --show-labels

For taints, list them per node:

  kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints

For anti-affinity, find where existing replicas already sit, since they are
what is blocking placement:

  kubectl get pods -l app=api -o wide

For topology spread, check whether whenUnsatisfiable is DoNotSchedule. With
that setting the constraint is hard, and a skew that cannot be satisfied leaves
the pod Pending rather than placing it unevenly.

## Remediation
1. Decide whether the rule or the cluster is wrong. A nodeSelector referring to
   a label that no longer exists is a stale rule, not a capacity problem.
2. If the label is simply missing from eligible nodes, add it:
   kubectl label node NODE_NAME disktype=ssd
3. If a taint is intentional, add the matching toleration to the pod spec
   rather than removing the taint, which would open the node to everything.
4. If anti-affinity is stricter than it needs to be, change
   requiredDuringSchedulingIgnoredDuringExecution to
   preferredDuringSchedulingIgnoredDuringExecution, so the scheduler treats it
   as a preference and places the pod anyway.
5. For topology spread, set whenUnsatisfiable: ScheduleAnyway, or add a node in
   the under-represented zone.
6. Confirm placement after the change:
   kubectl get pod POD_NAME -o wide

## Prevention
Keep the node label and taint taxonomy documented and applied by the node group
definition, not by hand. A manually labelled node loses its label when replaced.

Prefer preferred rules over required rules unless the constraint is a
correctness requirement. Spreading replicas is usually a preference; keeping
regulated data in one region is not.

Alert on pods Pending beyond a few minutes, and include the FailedScheduling
message in the alert so the two distinct causes are visible without a lookup.
