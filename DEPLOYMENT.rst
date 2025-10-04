Deployment Guide
================

This document provides comprehensive instructions for deploying the BGP data collection
system on a k3s cluster.

Overview
--------

The BGP data collection system is designed to run on a 3-node k3s cluster:

- **ctrl01.bgp-data.net**: Control Plane (no workloads).
- **node01.bgp-data.net**: Worker Node (workloads).
- **node02.bgp-data.net**: Worker Node (workloads).

Prerequisites
-------------

- 3 physical or virtual machines with:
  - Minimum 48 CPU cores.
  - 60GB RAM.
  - 500GB storage.
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

Control Plane Installation
-------------------------

On the control plane node (ctrl01.bgp-data.net):

.. code-block:: bash

   # Get the machine IP.
   MACHINE_IP=$(ip route get 1.1.1.1 | awk '{print $7; exit}')
   MACHINE_NET=$(ip -o -f inet addr show | awk -v ip=$MACHINE_IP '$4 ~ ip {print $4; exit}')

   # Add firewall rules.
   sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${MACHINE_NET} port port=6443 protocol=tcp accept"
   sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${MACHINE_NET} port port=8472 protocol=udp accept"
   sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${MACHINE_NET} port port=10250 protocol=tcp accept"
   sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source NOT address=${MACHINE_NET} port port=6443 protocol=tcp drop"
   sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source NOT address=${MACHINE_NET} port port=8472 protocol=udp drop"
   sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source NOT address=${MACHINE_NET} port port=10250 protocol=tcp drop"
   sudo firewall-cmd --reload

   # Install K3s Server.
   curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC=" \
      server \
      --node-name ctrl01.bgp-data.net \
      --node-ip ${MACHINE_IP} \
      --advertise-address ${MACHINE_IP} \
      --tls-san ctrl01.bgp-data.net \
      --tls-san ${MACHINE_IP} \
      " sh -s -
   
   # Print token for worker nodes.
   sudo cat /var/lib/rancher/k3s/server/node-token

Worker Node Installation
------------------------

On each worker node, install k3s as an agent:

.. code-block:: bash

   # K3s Token.
   K3S_TOKEN=<token>

   # Hostnames.
   HOSTNAME=$(hostname)
   HOSTNAME_CTRL=ctrl01.bgp-data.net
   
   MACHINE_IP=$(ip route get 1.1.1.1 | awk '{print $7; exit}')
   MACHINE_NET=$(ip -o -f inet addr show | awk -v ip=$MACHINE_IP '$4 ~ ip {print $4; exit}')

   # Add firewall rules.
   sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${MACHINE_NET} port port=6443 protocol=tcp accept"
   sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${MACHINE_NET} port port=8472 protocol=udp accept"
   sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source NOT address=${MACHINE_NET} port port=6443 protocol=tcp drop"
   sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source NOT address=${MACHINE_NET} port port=8472 protocol=udp drop"
   sudo firewall-cmd --reload

   # Install K3s Agent.
   curl -sfL https://get.k3s.io | \
      K3S_URL="https://$HOSTNAME_CTRL:6443" \
      K3S_TOKEN="${K3S_TOKEN}" \
      INSTALL_K3S_EXEC="agent --node-name $HOSTNAME --with-node-id" \
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