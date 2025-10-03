Deployment Guide
================

This document provides comprehensive instructions for deploying the BGP data collection
system on a k3s cluster with Tailscale connectivity.

Overview
--------

The BGP data collection system is designed to run on a 3-node k3s cluster:

- **ctrl01.bgp-data.net**: Control Plane (no workloads).
- **node01.bgp-data.net**: Worker Node (workloads).
- **node02.bgp-data.net**: Worker Node (workloads).

All nodes are connected via Tailscale for secure, encrypted communication.

Prerequisites
-------------

- 3 physical or virtual machines with:
  - Minimum 48 CPU cores.
  - 60GB RAM.
  - 500GB storage.
- Tailscale account and network setup.
- Internet connectivity on all nodes.

Node Preparation
----------------

Set hostnames on each machine:

.. code-block:: bash

   # Control plane
   hostnamectl set-hostname ctrl01.bgp-data.net
   
   # Worker node 1
   hostnamectl set-hostname node01.bgp-data.net
   
   # Worker node 2
   hostnamectl set-hostname node02.bgp-data.net

Tailscale Setup
---------------

Install Tailscale on all nodes:

.. code-block:: bash

   # Download and install Tailscale.
   curl -fsSL https://tailscale.com/install.sh | sh
   
   # Start Tailscale service.
   sudo systemctl enable --now tailscaled
   
   # Authenticate (run on each node).
   sudo tailscale up

Verify connectivity:

.. code-block:: bash

   ping ctrl01
   ping node01
   ping node02

Control Plane Installation
-------------------------

On the control plane node (ctrl01.bgp-data.net):

.. code-block:: bash

   # Install K3s.
   curl -sfL https://get.k3s.io | sh -s -

   # Get the Tailscale IP.
   TAILSCALE_IP=$(tailscale ip -4)

   # Add firewall rules.
   sudo firewall-cmd --permanent --zone=trusted --add-interface=tailscale0
   sudo firewall-cmd --permanent --zone=trusted --add-port=6443/tcp
   sudo firewall-cmd --permanent --zone=trusted --add-port=8472/udp
   sudo firewall-cmd --permanent --zone=trusted --add-port=10250/tcp
   sudo firewall-cmd --reload

   # Create config file.
   sudo mkdir -p /etc/rancher/k3s
   sudo tee /etc/rancher/k3s/config.yaml >/dev/null <<EOF
   node-name: ctrl01.bgp-data.net
   node-ip: ${TAILSCALE_IP}
   advertise-address: ${TAILSCALE_IP}
   tls-san:
     - ctrl01
     - ctrl01.bgp-data.net
     - ${TAILSCALE_IP}
   EOF
   
   # Print token for worker nodes.
   sudo cat /var/lib/rancher/k3s/server/node-token

Worker Node Installation
------------------------

On each worker node, install k3s as an agent:

.. code-block:: bash

   # K3s Token.
   K3S_TOKEN=<token>

   # Get the Tailscale IPs.
   TAILSCALE_IP=$(tailscale ip -4)
   TAILSCALE_IP_CTRL=$(getent hosts ctrl01 | awk '{ print $1 }')

   # Add firewall rules.
   sudo firewall-cmd --permanent --zone=trusted --add-interface=tailscale0
   sudo firewall-cmd --permanent --zone=trusted --add-port=8472/udp
   sudo firewall-cmd --permanent --zone=trusted --add-port=10250/tcp
   sudo firewall-cmd --reload

   # Install K3s Agent.
   curl -sfL https://get.k3s.io | \
      K3S_URL="https://${TAILSCALE_IP_CTRL}:6443" \
      K3S_TOKEN="${K3S_TOKEN}" \
      INSTALL_K3S_EXEC="agent --node-name node01.bgp-data.net --with-node-id" \
      sh -

Repeat for node02.bgp-data.net with appropriate node name.

Cluster Verification
--------------------

Verify cluster setup:

.. code-block:: bash

   # Check nodes.
   kubectl get nodes -o wide
   
   # Expected output:
   # NAME                    STATUS   ROLES                  AGE   VERSION
   # ctrl01.bgp-data.net     Ready    control-plane,master   5m    v1.28.2+k3s1
   # node01.bgp-data.net     Ready    <none>                 3m    v1.28.2+k3s1
   # node02.bgp-data.net     Ready    <none>                 3m    v1.28.2+k3s1

Taint Control Plane
-------------------

Prevent workloads from scheduling on the control plane:

.. code-block:: bash

   kubectl taint nodes ctrl01.bgp-data.net node-role.kubernetes.io/control-plane:NoSchedule

Application Deployment
-----------------------

Deploy the BGP data collection system:

.. code-block:: bash

   # Create namespace
   kubectl apply -f namespace.yaml
   
   # Create persistent volume claims
   kubectl apply -f pvc.yaml
   
   # Deploy services
   kubectl apply -f postgres.yaml
   kubectl apply -f kafka.yaml
   kubectl apply -f zookeeper.yaml
   kubectl apply -f aggregator.yaml
   kubectl apply -f whois.yaml
   kubectl apply -f web.yaml
   kubectl apply -f cloudflared.yaml
   kubectl apply -f collectors.yaml
   kubectl apply -f relays.yaml
   kubectl apply -f grafana.yaml

Verify Deployment
-----------------

Check service placement:

.. code-block:: bash

   # Verify pods are running on correct nodes.
   kubectl get pods -o wide --namespace=bgpdata
   
   # Expected distribution:
   # - node01.bgp-data.net: postgres, kafka, collectors, relays
   # - node02.bgp-data.net: zookeeper, web, aggregator, whois, cloudflared
   # - ctrl01.bgp-data.net: no application pods

Check persistent volumes:

.. code-block:: bash

   # Verify PVCs are bound.
   kubectl get pvc --namespace=bgpdata
   
   # All PVCs should show STATUS: Bound

Service Access
--------------

Access services through Tailscale:

.. code-block:: bash

   # Web interface.
   curl http://node02:8080
   
   # Grafana dashboard.
   curl http://node02:3000
   
   # PostgreSQL (from within cluster).
   kubectl exec -it postgres-<pod-id> -- psql -U bgpdata -d bgpdata

Monitoring
----------

Monitor cluster health:

.. code-block:: bash

   # Check node status.
   kubectl top nodes
   
   # Check pod resource usage.
   kubectl top pods --namespace=bgpdata
   
   # Check persistent volume usage.
   kubectl get pv

Troubleshooting
---------------

Common issues and solutions:

**Node not joining cluster:**
   - Verify Tailscale connectivity.
   - Check firewall rules.
   - Ensure correct token and IP.

**Pods not starting:**
   - Check node affinity rules.
   - Verify persistent volume claims.
   - Review pod logs: ``kubectl logs <pod-name>``.

**Volume issues:**
   - Verify storage class configuration.
   - Check available disk space.
   - Review PVC status.

Maintenance
-----------

**Updating services:**
   - Modify manifests as needed.
   - Apply changes: ``kubectl apply -f <manifest>``.
   - Services will maintain their node placement.

**Backup persistent data:**
   - Backup volumes before major changes.
   - Use appropriate backup tools for your storage backend.

**Scaling:**
   - Add new nodes with appropriate hostnames.
   - Update node affinity rules if needed.
   - Rebalance workloads as required.

Security Considerations
-----------------------

- All inter-node communication is encrypted via Tailscale.
- Control plane is isolated from workloads.
- Persistent volumes maintain data integrity.
- Regular security updates recommended.

For additional support, refer to the k3s documentation and Tailscale networking guides.
