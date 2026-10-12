# XRPL spike environment

## Pinned build

- `xrpld version 3.4.0`
- Git commit: `4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`
- Git build branch reported by the binary: `HEAD`
- Debian package: `xrpld 3.4.0-1 amd64`
- Package URL: `https://packages.xrplf.org/repository/deb-stable/pool/x/xrpld/xrpld_3.4.0-1_amd64.deb`
- Package SHA-256: `4dc40a21b12b7d21f2760b16545c1d842962a96978dd3199eb8de9aa0aff8339`
- Installed binary SHA-256: `9b4d3dd28b31a22419fd905682e238298eefb8941e2efcde5241a4b19fa0c417`
- Source tag checked for provenance: `3.4.0`, resolving to the same Git commit.

The image uses the pinned `linux/amd64` Debian 12 slim base manifest
`sha256:a4672c0cb26fbdde88e38fa2dfb6c681942306680e41e4378b28770b6e79ee91`.
The locally built image ID/repository digest is
`sha256:cb3c82ba3e4b455621fd006d6ac1198269cf95e7b48038df46a659a695971448`.
The Docker build verifies the downloaded package checksum before installation.

## Invocation and persistence

The container entrypoint is `/usr/bin/xrpld` and its command is:

```text
--conf=/etc/opt/xrpl/xrpld.cfg --standalone --start
```

RPC listens only on host `127.0.0.1:5005`. The named Docker volume
`credence-xrpl-spike-data` is mounted at `/var/lib/xrpl`. The server was
started from a new empty volume for this run. No custom genesis file was used;
`xrpld --standalone --start` created the built-in fresh standalone genesis.
The stopped ledger remained at validated ledger index 2. No protocol
transaction had been submitted.

The standalone genesis account seed was supplied to the process at runtime and
was not written to any artifact. Runtime actor identities were generated only
in process. The amendment gate stopped before those actors were funded or used.

## Ledger configuration

The exact configuration is `config/xrpld.cfg`, whose SHA-256 is
`37ffc6f93f7d8838aef681e4a3c7e39d7427e101cb6c62113dca208dc1e8e820`.
It specifies:

- standalone network ID `21337`;
- NuDB under `/var/lib/xrpld/db/nudb`;
- the eight required default-no amendments by exact pinned-release ID;
- explicit ledger startup with `--start`;
- ledger close mechanism for the planned experiments: the administrative
  `ledger_accept` RPC.

The planned close mechanism was not exercised in the stopped run. The only
validated ledger was the fresh standalone startup ledger at index 2.

## Amendment state

The complete `feature` response normalized to explicit booleans is in
`evidence/amendments.json`. It contains 106 amendments: 9 enabled and 97
disabled. The nine enabled amendments were the eight required entries plus
`fixRemoveNFTokenAutoTrustLine`.

| Required amendment | ID | Supported | Enabled |
| --- | --- | --- | --- |
| MPTokensV1 | `950AE2EA4654E47F04AA8739C0B214E242097E802FD372D24047A89AB1F5EC38` | yes | yes |
| SingleAssetVault | `81BD2619B6B3C8625AC5D0BC01DE17F06C3F0AB95C7C87C93715B87A4FD240D8` | yes | yes |
| LendingProtocol | `565B90CA1AB2B9D42208ED10884188C64F9E19083DECB9634AAF06EB03299509` | yes | yes |
| LendingProtocolV1_1 | `A360E2BFD775A5B0DCE1C36C16DF31B72735A57584FD163655D2F9564F8E7AC8` | yes | yes |
| fixCleanup3_1_3 | `303ACB16CF8DBD3B5C34F131A9D19A7DE01AE05F480A8A682B869D1B4AAC8CFC` | yes | yes |
| fixCleanup3_2_0 | `21B8D2F76F68E11E9C077A43BBBC394136E9987E99DDB73966DD68419467E431` | yes | yes |
| fixCleanup3_3_0 | `3298D47E1F3A8A24FECAA30F699B8FE1DD234E072834BA099AD8180FFCE0FEC4` | yes | yes |
| fixCleanup3_4_0 | `98433DD001A5737F773D74F8CA2A25A065089C73B2E611C760BAF369E4FECA76` | yes | yes |

The pinned source's `include/xrpl/protocol/detail/features.macro` declares all
eight. The current `feature` RPC independently reports all eight as supported.

## Checkpoint 1A recovery observation

Checkpoint 1A retained the identical container, image, configuration, network
ID, and pinned build. The corrected E1-only validator called `ledger_accept`
twice. The validated ledger advanced exactly from index 2 to 3 and then 4; both
calls returned `success`. The required amendment state and server identity were
unchanged across both closes.

Raw `feature` responses captured immediately before and after the closes are:

- `evidence/feature-raw-e1-before.json`, SHA-256
  `3f9caa62f7b9c340207c49ed6f16fadbd605bb8b068332a4f358957f5ece3cd8`;
- `evidence/feature-raw-e1-after.json`, SHA-256
  `3f9caa62f7b9c340207c49ed6f16fadbd605bb8b068332a4f358957f5ece3cd8`.

Their identical digests provide byte-for-byte evidence that the complete
amendment response persisted across the closes. The machine-readable E1 result
and the independently checkable digests are in `evidence/e1-validation.json`.

## Checkpoint 1B fresh-start observation

Checkpoint 1B rebuilt the same pinned package and recreated both the dedicated
container and named volume. Docker inspection showed the volume mounted at
`/var/lib/xrpld`, correcting the earlier documented mismatch with the configured
NuDB/database paths. The command remained
`--conf=/etc/opt/xrpl/xrpld.cfg --standalone --start`; the container began at
`2026-10-11T07:40:06.290957026Z` with network ID `21337` and fresh validated
ledger index 2.

The existing strict `feature` validation succeeded, but a newly added
`ledger_entry` cross-check used `amendments: {}` and received
`malformedRequest`. The checkpoint stopped before a ledger close or protocol
transaction. This observation does not replace the Checkpoint 1A E1 PASS; it
records that the new 1B regression gate is BLOCKED by a local request-shape
defect.

## Checkpoint 1B-R repaired E1

Pinned 3.4.0 source establishes the fixed-location request shape as
`{"ledger_index":"validated","amendments":true}`. The repaired runner used
that exact shape against a newly recreated volume mounted at `/var/lib/xrpld`.

The environment again reported xrpld `3.4.0`, package `3.4.0-1 amd64`, source
commit `4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`, standalone network ID
`21337`, and `--start`. Two explicit closes advanced ledger indices 2 → 3 → 4.

The `feature` response and Amendments ledger object independently confirmed
all eight required amendments before and after the closes. Their normalized
cross-check had no disagreements. Raw responses, complete build provenance,
and the evidence digest manifest are in `evidence/checkpoint-1b-r/`.
