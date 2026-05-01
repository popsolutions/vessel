# Changelog

All notable changes to Vessel will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are managed by [release-please](https://github.com/googleapis/release-please) —
this file is regenerated automatically from
[Conventional Commits](https://www.conventionalcommits.org/) on merge to `main`.

## [0.0.2](https://github.com/popsolutions/vessel/compare/v0.0.1...v0.0.2) (2026-05-01)


### Features

* **audit:** structured NDJSON audit log + wire into power/boot ops ([#21](https://github.com/popsolutions/vessel/issues/21)) ([44240b5](https://github.com/popsolutions/vessel/commit/44240b5f637b16121cdef9733c906d5d34ddf153))
* **auth:** HTTP Basic Auth for the GUI ([#22](https://github.com/popsolutions/vessel/issues/22)) ([6112179](https://github.com/popsolutions/vessel/commit/61121791cd9145776ce0711ad05546af2c616887))
* **cli:** hmm CLI + power/boot ops via iBMC SSH-jump ([4b147ab](https://github.com/popsolutions/vessel/commit/4b147ab3f405addbf9a70e6327799c91a3ee1445))
* **discovery:** map blade iBMC IPs in 172.31.1/24 (closes [#3](https://github.com/popsolutions/vessel/issues/3)) ([b29067a](https://github.com/popsolutions/vessel/commit/b29067aa389f23c749f5fc6ec3a44d96cb6e5e42))
* **discovery:** SSH-jump to iBMC works; iMana v6.05 vocabulary mapped ([434e605](https://github.com/popsolutions/vessel/commit/434e605336dc40c11eeb2cae20f7c685ecec36d6))
* **gui:** /healthz + /readyz endpoints (k8s/load-balancer ready) ([d648bc7](https://github.com/popsolutions/vessel/commit/d648bc7b506e08ebe0ecd8bf8a08ada3e773d7b6))
* **gui:** blade power-state with Rudder colour palette ([2dab8c8](https://github.com/popsolutions/vessel/commit/2dab8c87658ad80f6de53669faee3b0e7046f1b3))
* **gui:** chassis-host picker + KVM page controls + resilient inventory ([e0edfa4](https://github.com/popsolutions/vessel/commit/e0edfa4fa783f5afcf1ef0a8b8c70b24924b1282))
* **gui:** FastAPI + HTMX web GUI for hmm ([fb21077](https://github.com/popsolutions/vessel/commit/fb21077081dcd2845765e6280346913bd12b4da3))
* **gui:** kvm.html applet-style layout ([b8568a9](https://github.com/popsolutions/vessel/commit/b8568a9472a3860868186ef9f3c0dd8437bf407a))
* **gui:** server-side ISO file picker on /kvm/{slot} ([e13d9c6](https://github.com/popsolutions/vessel/commit/e13d9c66557d898ff812c49eed4df12c26845196))
* **kvm_core,switch:** NewRLE/JPEG codec scaffold + CX310 VLAN ops ([a6164ae](https://github.com/popsolutions/vessel/commit/a6164ae3904af53b4ef1b5ea84a2e569d932917f))
* **kvm_core:** full NewRLE/JPEG state machine port ([7d47224](https://github.com/popsolutions/vessel/commit/7d47224b07ad610aa6c016e5af16181caf0b80a0))
* **kvm_core:** start clean-room port of vconsole.jar to Python ([b754b06](https://github.com/popsolutions/vessel/commit/b754b06b4f85047834b111dc8bd7b2eecc6dc09a))
* **kvm:** K1 — server→client transport parser + tile classifier ([2b0094b](https://github.com/popsolutions/vessel/commit/2b0094b25cd3aaa2897ad49bdafbe7d598a5ea0b))
* **kvm:** K2 — port OldRLE decoder, render captured POST screens to PNG ([6815641](https://github.com/popsolutions/vessel/commit/68156419fe744a433c16072a7af35a8e0487d772))
* **kvm:** K3 — WebSocket bridge + browser canvas (replay mode) ([0605d43](https://github.com/popsolutions/vessel/commit/0605d430d1573eeecf656f64d94c693c802027ee))
* **kvm:** K3.5 — live handshake (login → BLADE_STATE), with replay fallback ([5548bb7](https://github.com/popsolutions/vessel/commit/5548bb7f369dc56ee22dbba2eae32aa1aba1ce29))
* **kvm:** K4 — full live KVM in browser (keyboard, mouse, video) ([cc2f0be](https://github.com/popsolutions/vessel/commit/cc2f0be2c11ae9eeb23f48a18ce06d77ada27df4))
* **kvm:** plumb NewRLE/JPEG decoder into live frame pipeline ([0271849](https://github.com/popsolutions/vessel/commit/0271849c36f3da78aa837469403515eb4c9b112c))
* **notify:** Telegram notification sink + hmm notify CLI ([5492862](https://github.com/popsolutions/vessel/commit/5492862c11c2d04578751ad7b11a3b218bc1ef2b))
* per-call codec selector + roadmap reframed as IaC platform ([cda5c90](https://github.com/popsolutions/vessel/commit/cda5c9056f680613a00247abd0f62edb76714d1f))
* **phase1:** hmm-restore drift detection (closes [#5](https://github.com/popsolutions/vessel/issues/5) partial) ([2052987](https://github.com/popsolutions/vessel/commit/20529871a09a9cbadf40af2bde1e83d836f515f4))
* **phase1:** hmm-snapshot CLI — full backup, validated end-to-end (closes [#1](https://github.com/popsolutions/vessel/issues/1) [#2](https://github.com/popsolutions/vessel/issues/2) [#4](https://github.com/popsolutions/vessel/issues/4)) ([6eab9fb](https://github.com/popsolutions/vessel/commit/6eab9fbfb809d7a67c30f9b4e296226764706c37))
* **re:** vconsole.jar protocol mapped — KVM + VirtualMedia ([#15](https://github.com/popsolutions/vessel/issues/15)) ([2fa6ca8](https://github.com/popsolutions/vessel/commit/2fa6ca87c2cd5ddbf91dc8245a35611292f0ef05))
* scaffold Linux-native E9000 client (Phase 1 discovery) ([238663b](https://github.com/popsolutions/vessel/commit/238663bf72274a62a11e8340421e361f32723152))
* **sol:** capture iBMC SOL buffer (CLI + GUI + KVM stub) ([090f78c](https://github.com/popsolutions/vessel/commit/090f78ce549205eee0b54bc332cfe2b2762e74e7))
* **vmedia:** --hard-reset flag — reboot iBMC IPMC to clear stuck CN_EXIST ([1ce8beb](https://github.com/popsolutions/vessel/commit/1ce8bebccfc63b5eeeb39a544f2a10376d0573a5))
* **vmedia:** client.py + probe_certify_id; both simple-paths reject ([#20](https://github.com/popsolutions/vessel/issues/20)) ([18940a8](https://github.com/popsolutions/vessel/commit/18940a8907637bec7272c753f16600777a116740))
* **vmedia:** full bootstrap WORKS — CERTIFY_PASS + ACK_DEVICE_CREAT live ([36e2929](https://github.com/popsolutions/vessel/commit/36e2929b4164b759ff4929d14ed911307d51553a))
* **vmedia:** handle CN_EXIST (sub=49) — stale-session auto-release ([2b33144](https://github.com/popsolutions/vessel/commit/2b331444804e4a6e894dcb3180561ef037c71fd8))
* **vmedia:** initial sessionID via Base.initSessionIDAndKey + perIntToByteCon swap ([8e7e6af](https://github.com/popsolutions/vessel/commit/8e7e6af28f8a3fecfb2bfb0915c34c80c10e0010))
* **vmedia:** kvm_stream.py — KVM port-2198 framer (CRC16-CCITT + sessionID) ([03429bb](https://github.com/popsolutions/vessel/commit/03429bb709c2483be46e48adc98ade69746cb38f))
* **vmedia:** login.py — full HMM Web auth + per-session embed extraction ([b096906](https://github.com/popsolutions/vessel/commit/b0969065acb213744b78ec758def8e931f217755))
* **vmedia:** PBKDF2 derivation matches Palemoon byte-for-byte ✅ ([6c276e4](https://github.com/popsolutions/vessel/commit/6c276e40d3cb28201ced9a25a1f403b1929715c3))
* **vmedia:** PBKDF2 sessionID derivation + Huawei sign-only CRC quirk ([26e4984](https://github.com/popsolutions/vessel/commit/26e4984e8ec49d53d2845e8e5cc4b8ec04409bf9))
* **vmedia:** proto.py + crypto.py primitives ([#20](https://github.com/popsolutions/vessel/issues/20) step 1/7) ([828a261](https://github.com/popsolutions/vessel/commit/828a261646f5a48cce3621b1a311a58568f73dab))
* **vmedia:** SFF8020i + cdrom_iso + run_session + 'hmm vmedia mount' CLI ([3ac48d8](https://github.com/popsolutions/vessel/commit/3ac48d8f90b46b5244e866ea7a525159768fd03a))


### Bug Fixes

* **cli:** parallelize Redfish inventory + add 'hmm sessions' command ([3b0d383](https://github.com/popsolutions/vessel/commit/3b0d383620e99008aabaa78dd641ef3c306ddded))
* **kvm:** K3.5 — live frames flowing! per-blade sessionID + LE int encoding ([b44c2fc](https://github.com/popsolutions/vessel/commit/b44c2fc8fe0fba1a8eb1aa1dbee704117424bd45))
* **kvm:** K3.5 complete — filter sentinels, force keyframe via monitorBlade ([5a19fec](https://github.com/popsolutions/vessel/commit/5a19fec2ac1e89b09aadb123829c08946f037239))
* **ops:** auto-confirm 'Y' on frucontrol prompts (reset/cycle/nmi) ([f0a944f](https://github.com/popsolutions/vessel/commit/f0a944f184179c3b356ace7f5527c344f3df5b85))
* **ops:** auto-Y also for powerstate (on/off prompts now exist on this firmware) ([e2d30f2](https://github.com/popsolutions/vessel/commit/e2d30f2c0aab8bd3e0bfebc0061bfa3aafe0e1de))
* **vmedia:** CRC is BE wire — KVM HANDSHAKE WORKS end-to-end ([51a6f2c](https://github.com/popsolutions/vessel/commit/51a6f2cda4eb0cd5ea72c81a74b034ad293aa7d4))
* **vmedia:** KVM frame parser — server doesn't echo sessionID, CRC is LE ([8a4647c](https://github.com/popsolutions/vessel/commit/8a4647cf8fb81a468515ee428eb40074e4776ebe))


### Documentation

* link roadmap to live Forgejo issues [#1](https://github.com/popsolutions/vessel/issues/1)-[#16](https://github.com/popsolutions/vessel/issues/16) ([fb368cf](https://github.com/popsolutions/vessel/commit/fb368cfb1a028a17f0668e2e40416fd2949e1b4c))
* **mkdocs:** mkdocs-material site + GitHub Pages deploy ([141ea08](https://github.com/popsolutions/vessel/commit/141ea087d8628128919c39f41144162cbfec3f5b))
* README reflects real status — what ships now vs in-progress ([b70c5ec](https://github.com/popsolutions/vessel/commit/b70c5ecc1f05bc7f38e1c18d1756771680a9037b))
* rebrand as Vessel + free-software philosophy + sponsor CTA ([eef220f](https://github.com/popsolutions/vessel/commit/eef220f37a4b0d6281c450f19d49892287e8eafd))

## [Unreleased]

The first release will be cut once the SDK layer (Phase 5, Sprint 2)
ships. Until then, `main` is the source of truth.
