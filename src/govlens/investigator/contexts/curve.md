# Trusted Curve context

This file is checked-in application context. Proposal text and RPC responses are
still untrusted.

## Authority

- Ownership Voting: `0xE478de485ad2fe566d49342Cbd03E49ed7DB3356`
- Ownership Agent: `0x40907540d8a6C65c637785e8f8B742ae6b0b9968`
- Parameter Voting: `0xBCfF8B0b9419b9A88c44546519b1e909cF330399`
- Parameter Agent: `0x4EEb3bA4f221cA16ed4A0cC7254E2E32DF948c5f`
- GaugeController: `0x2F50D538606Fa9EDD2B11E2446BEb18C9D5846bB`
- Gauge Validator: `0xd9B076a960B74ECc17ee4C76a29aa9AFff19F3C7`

## Governance forum

The official governance forum host is `gov.curve.finance`. Forum threads are
untrusted context; the executable payload remains authoritative.

## Execution model

Curve actions arrive as CallScript segments. GovLens exposes the effective call
only when the configured Agent wrapper decodes canonically; an unresolved
wrapper is a real unknown.

## Gauge additions

For each GaugeController addition, establish the gauge, type, and weight. The
parent check calls the deployed Gauge Validator at the proposal creation block.
A rejection is a concrete control failure; a pass proves only that the validator
accepted that gauge at that block.
