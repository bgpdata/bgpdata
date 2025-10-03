<img title="BGPDATA" src="logo.svg" height="64" align="left" />

<br />
<br />

---

[https://bgp-data.net](https://bgp-data.net/?ref=github.com) — A BGP (Border Gateway Protocol) Data Aggregation Service.

BGPDATA provides researchers with real-time and historical visibility into the Internet by collecting and analyzing Border Gateway Protocol (BGP) messages from [Route Views](https://www.routeviews.org/), [RIPE NCC RIS](https://ris.ripe.net), as well as our own collector infrastructure. In practice, it delivers a real-time, searchable map of how all networks across the globe route traffic and interconnect to form the Internet.

## Data Flow

<img src="dataflow.png" height="450" />

## Prerequisites

Before you begin, ensure you have the following installed on your system:

-   [Docker](https://docs.docker.com/get-docker/)
-   [Docker Compose](https://docs.docker.com/compose/install/)
-   [Git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)

## Getting Started

1. Clone the repository:
```bash
git clone --recurse-submodules git@github.com:bgpdata/bgpdata.git
cd bgpdata
```

2. Start the project:
```sh
docker compose up
```

> **Note:** This will start collecting from the defined collectors, a startup may take hours until the database has a full view of the current state of the global routing table.

4. Open:<br>
http://localhost:8080 (Web)<br>
http://localhost:3000 (Grafana)<br>
and try `whois -h localhost AS3582` (WHOIS)

## Production Deployment

The recommended system requirements are a Manager and Worker Node with each 60 GB of RAM, 60 GB Swap, 1 TB Datacenter SSD Storage and 48 vCPU cores. Initial system provisioning may require up to 3 hours to complete, contingent upon your specific configuration parameters. Once initialization is complete, resource utilization will stabilize at optimal levels.

```sh
# 
kubectl apply -f k8s/
```

# ACKs

-   [Route Views](https://www.routeviews.org/) for providing the data and collector infrastructure
-   [OpenBMP](https://www.openbmp.org/) for the invaluable OpenBMP Suite used in-depth in this project
-   [Tim Evens](https://github.com/TimEvens) for his leading role in the development of the OpenBMP
-   [RIPEstat](https://stat.ripe.net/) for their incredible infrastructure and data visualization
-   [RIPE Atlas](https://atlas.ripe.net/) for providing the RIPE Atlas measurement infrastructure
-   [Postmark](https://postmarkapp.com/) for providing the email service

## License

See [LICENSE](LICENSE)
