# Proposal Summary

## Project title

Sensor interoperability bridge for the Geonovum SensorThings testbed.

## Objective

This project demonstrates how heterogeneous wastewater-monitoring sensors can be connected to a central OGC SensorThings API server in a reproducible and standards-aligned way.

The proposed approach is tailored to the Brabantse Delta digital twin use case and focuses on practical interoperability between mixed sensor types, including analog sensors and Modbus-based pump telemetry.

## Proposed approach

We use a lightweight bridge architecture:

- field sensors or simulated sensor feeds produce measurements
- Node-RED acts as the intermediary integration layer
- the connector service transforms incoming readings into SensorThings-compatible payloads
- the central FROST server stores and exposes the observations for query and analysis

This approach is deliberately simple, transparent, and reproducible for other public-sector use cases.

## Why this is a good fit

- well suited to mixed sensor landscapes
- low implementation risk
- easy to demonstrate publicly
- aligned with OGC SensorThings and OM Measurement concepts
- reusable for future digital twin and monitoring scenarios

## Main deliverables

- working connection from one or more sensors to the central SensorThings server
- validation of timing, structure, and reliability
- public demonstration setup
- open repository with MIT-licensed source code and reproducible documentation
- lessons learned report published under CC BY 4.0

## Implementation focus

The proof of concept emphasizes:

- interoperability over custom platform building
- reproducibility over heavy infrastructure
- practical integration with existing Node-RED-based setups

## Expected result

A compact but credible demonstration that sensor data from the wastewater use case can be standardized and exchanged through the OGC SensorThings API, helping reduce interoperability barriers in the Dutch public sector.
