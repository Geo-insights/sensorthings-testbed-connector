# Node-RED Quickstart

## Purpose

This guide shows how to use Node-RED as a bridge between sensor sources and the SensorThings testbed connector.

Node-RED is a secondary integration path. The primary data sources (Kafka TGV, Ohnics, Levellog) use background polling loops built into the connector. Node-RED is useful when you need to bridge additional sensor feeds that are not covered by the built-in sources.

## Why Node-RED is useful here

- it is easy to connect different sensor sources
- it is well suited for rapid prototyping
- it can transform raw readings into a clean JSON payload
- it can forward those readings to the connector service or directly to a FROST server

## Starter flow included

An importable example flow is available here:

- [examples/node-red/sensorthings-bridge-flow.json](../examples/node-red/sensorthings-bridge-flow.json)

## How to use it

### 1. Import the flow

In Node-RED:

- open the menu
- choose Import
- paste the JSON from the sample flow file or select the file directly

### 2. Start the local connector

Make sure the local connector app is running on port 8010.

### 3. Trigger the demo flow

Click the inject node named Send demo readings.

This will send a bridge payload with climate-adaptation readings, for example:

- air temperature
- relative humidity

The sample bridge payload at [examples/node-red/bridge-payload.json](../examples/node-red/bridge-payload.json) shows the expected format with `sensor_id`, `sensor_name`, `observed_property`, `unit`, `value`, `timestamp`, `quality`, `location`, `thing_name`, and optional `device_eui`, `stream_key`, `observed_property_name` fields.

### 4. Inspect the result

The response appears in the Node-RED debug panel.

By default the flow posts to the preview endpoint so you can test safely.

## Switching from preview to live mode

Once the FROST server details are available:

1. set the SensorThings base URL in the connector environment
2. call the registration endpoint once to create demo entities
3. change the Node-RED HTTP request target from the preview endpoint to the live ingest endpoint
