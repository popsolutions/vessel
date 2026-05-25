# Screenshots

UI screenshots for Vessel docs and the project README live here.

## Where they're referenced

Docs pages and the top-level README link to images in this folder via
relative paths. Some examples already wired up:

| Filename | Used by | What it should show |
|---|---|---|
| `login.png` | `docs/operations/gui-auth.md` | The full `/login` form with target dropdown collapsed |
| `target-switcher.png` | `docs/operations/multi-chassis.md` | Top-left pill on a chassis page (e.g. `▾ 192.168.1.30`) next to the open dropdown |
| `add-new-chassis.png` | `docs/operations/multi-chassis.md` | `/login` after picking "+ add new chassis…", with the text input visible |
| `chassis-home.png` | `README.md` | Main chassis page with the cabinet sprite, blade grid populated |
| `kvm-canvas.png` | `README.md` | Embedded KVM canvas showing a live console (e.g. Proxmox boot, BIOS, login prompt) |
| `firmware-upgrade.png` | `README.md` | `/firmware-web` upgrade panel with inventory + apply button |

Drop new images into this folder using the filenames above and they
will render in the docs site (mkdocs serves `docs/` as the site root)
and on github.com (relative links resolve in the rendered README).

## Conventions

- **Format:** PNG preferred (lossless, no JPEG artifacts on text).
  SVG ok for diagrams.
- **Dimensions:** width 1200-1600px is comfortable for both the
  mkdocs site and the README on github.com. Anything larger gets
  scaled by the browser; smaller starts to blur.
- **No real credentials.** Crop or redact anything that shows real
  hostnames, IPs from production networks, MAC addresses, asset
  tags, or operator usernames. The illustrative IPs `192.168.1.30`
  and `192.168.1.20` are already public and fine to leave visible.
- **Dark theme** matches the GUI's default palette and the docs
  site's `slate` scheme — capture in dark mode unless you're
  specifically documenting a light-mode feature.
- **Annotate sparingly.** If you need to point at something, a
  single red rectangle or arrow is enough; avoid layering text on
  top of the UI.

## Adding more

When you add a new screenshot:

1. Drop the PNG into this folder.
2. Reference it from the relevant doc with `![alt](../screenshots/your-file.png)`
   (or `docs/screenshots/your-file.png` from the README at the
   repo root).
3. Add a row to the table above so the next maintainer knows what
   the image is supposed to show.
