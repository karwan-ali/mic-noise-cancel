//! Tray and program icon artwork, drawn in code so the app ships no image files.
//!
//! A rounded-square badge with a microphone glyph. Each pixel is the coverage
//! of a 4x4 grid of sample points, which gives clean edges at 16 px as well as
//! at 256 px. Also compiled into build.rs, to produce the .ico for the .exe.

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum IconState {
    Active,
    Bypassed,
    Stopped,
    Error,
}

type Rgb = (u8, u8, u8);

fn palette(state: IconState) -> (Rgb, Rgb) {
    match state {
        IconState::Active => ((0x2E, 0xC4, 0x7B), (0xFF, 0xFF, 0xFF)),
        IconState::Bypassed => ((0x8A, 0x92, 0x9E), (0xFF, 0xFF, 0xFF)),
        IconState::Stopped => ((0x6B, 0x72, 0x80), (0xE5, 0xE7, 0xEB)),
        IconState::Error => ((0xE0, 0x4F, 0x5F), (0xFF, 0xFF, 0xFF)),
    }
}

/// Inside a rectangle with rounded corners (coordinates in unit square).
fn in_round_rect(x: f32, y: f32, x0: f32, y0: f32, x1: f32, y1: f32, r: f32) -> bool {
    if x < x0 || x > x1 || y < y0 || y > y1 {
        return false;
    }
    let cx = x.clamp(x0 + r, x1 - r);
    let cy = y.clamp(y0 + r, y1 - r);
    (x - cx).powi(2) + (y - cy).powi(2) <= r * r
}

fn dist_to_segment(px: f32, py: f32, ax: f32, ay: f32, bx: f32, by: f32) -> f32 {
    let (dx, dy) = (bx - ax, by - ay);
    let t = (((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)).clamp(0.0, 1.0);
    ((px - ax - t * dx).powi(2) + (py - ay - t * dy).powi(2)).sqrt()
}

/// Which layer a sample point falls in: 0 none, 1 body, 2 accent, 3 dark outline.
fn sample(state: IconState, x: f32, y: f32) -> u8 {
    if !in_round_rect(x, y, 0.0, 0.0, 1.0, 1.0, 0.24) {
        return 0;
    }
    let slash = state == IconState::Bypassed;
    if slash {
        let d = dist_to_segment(x, y, 0.20, 0.80, 0.80, 0.20);
        if d <= 0.0225 {
            return 2;
        }
        if d <= 0.045 {
            return 3;
        }
    }
    let cx = 0.5;
    // Capsule
    let (cap_w, cap_h, top) = (0.26, 0.40, 0.17);
    if in_round_rect(x, y, cx - cap_w / 2.0, top, cx + cap_w / 2.0, top + cap_h, cap_w / 2.0) {
        return 2;
    }
    // Cradle: the lower half of an ellipse outline.
    let arc_w = 0.44;
    let arc_top = top + cap_h * 0.42;
    let arc_bottom = top + cap_h * 1.16;
    let (ecx, ecy) = (cx, (arc_top + arc_bottom) / 2.0);
    let (rx, ry) = (arc_w / 2.0, (arc_bottom - arc_top) / 2.0);
    let stroke = 0.055;
    if y >= ecy {
        let outer = ((x - ecx) / rx).powi(2) + ((y - ecy) / ry).powi(2);
        let inner = ((x - ecx) / (rx - stroke)).powi(2) + ((y - ecy) / (ry - stroke)).powi(2);
        if outer <= 1.0 && inner >= 1.0 {
            return 2;
        }
    }
    // Stem and base
    let stem_w = 0.055;
    if (x - cx).abs() <= stem_w / 2.0 && y >= arc_bottom - 0.03 && y <= 0.83 {
        return 2;
    }
    let base_w = 0.28;
    if in_round_rect(x, y, cx - base_w / 2.0, 0.80, cx + base_w / 2.0, 0.855, stem_w / 2.0) {
        return 2;
    }
    1
}

/// Straight (non-premultiplied) RGBA, row-major, top row first.
pub fn render_rgba(state: IconState, size: u32) -> Vec<u8> {
    const SS: u32 = 4;
    let (body, accent) = palette(state);
    let dark: Rgb = (0x22, 0x26, 0x2C);
    let mut out = Vec::with_capacity((size * size * 4) as usize);
    for py in 0..size {
        for px in 0..size {
            let (mut r, mut g, mut b, mut a) = (0u32, 0u32, 0u32, 0u32);
            for sy in 0..SS {
                for sx in 0..SS {
                    let x = (px as f32 + (sx as f32 + 0.5) / SS as f32) / size as f32;
                    let y = (py as f32 + (sy as f32 + 0.5) / SS as f32) / size as f32;
                    let c = match sample(state, x, y) {
                        0 => continue,
                        1 => body,
                        2 => accent,
                        _ => dark,
                    };
                    r += c.0 as u32;
                    g += c.1 as u32;
                    b += c.2 as u32;
                    a += 1;
                }
            }
            match (r.checked_div(a), g.checked_div(a), b.checked_div(a)) {
                (Some(r), Some(g), Some(b)) => {
                    out.extend_from_slice(&[r as u8, g as u8, b as u8, (a * 255 / (SS * SS)) as u8])
                }
                _ => out.extend_from_slice(&[0, 0, 0, 0]),
            }
        }
    }
    out
}

/// A multi-resolution .ico file (32-bit BMP entries) of the active icon.
#[allow(dead_code)] // used by build.rs only
pub fn ico_file(sizes: &[u32]) -> Vec<u8> {
    let mut images = Vec::new();
    for &s in sizes {
        let rgba = render_rgba(IconState::Active, s);
        let mut img = Vec::new();
        // BITMAPINFOHEADER; height is doubled for the (unused) AND mask.
        img.extend_from_slice(&40u32.to_le_bytes());
        img.extend_from_slice(&(s as i32).to_le_bytes());
        img.extend_from_slice(&((s * 2) as i32).to_le_bytes());
        img.extend_from_slice(&1u16.to_le_bytes());
        img.extend_from_slice(&32u16.to_le_bytes());
        img.extend_from_slice(&[0u8; 24]);
        for row in (0..s).rev() {
            for col in 0..s {
                let i = ((row * s + col) * 4) as usize;
                img.extend_from_slice(&[rgba[i + 2], rgba[i + 1], rgba[i], rgba[i + 3]]);
            }
        }
        let mask_row = (s.div_ceil(32) * 4) as usize;
        img.extend(std::iter::repeat_n(0u8, mask_row * s as usize));
        images.push((s, img));
    }

    let mut out = Vec::new();
    out.extend_from_slice(&[0, 0, 1, 0]);
    out.extend_from_slice(&(images.len() as u16).to_le_bytes());
    let mut offset = 6 + 16 * images.len() as u32;
    for (s, img) in &images {
        let dim = if *s >= 256 { 0 } else { *s as u8 };
        out.extend_from_slice(&[dim, dim, 0, 0]);
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&32u16.to_le_bytes());
        out.extend_from_slice(&(img.len() as u32).to_le_bytes());
        out.extend_from_slice(&offset.to_le_bytes());
        offset += img.len() as u32;
    }
    for (_, img) in images {
        out.extend_from_slice(&img);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn draws_every_state() {
        for state in [IconState::Active, IconState::Bypassed, IconState::Stopped, IconState::Error] {
            let px = render_rgba(state, 32);
            assert_eq!(px.len(), 32 * 32 * 4);
            assert_eq!(px[3], 0, "corner is transparent");
            let centre = (16 * 32 + 16) * 4;
            assert_eq!(px[centre + 3], 255, "centre is opaque");
        }
        let ico = ico_file(&[16, 32]);
        assert_eq!(&ico[..6], &[0, 0, 1, 0, 2, 0]);
    }
}
