/// Grid layout — computes cell rectangles for N sessions in a terminal.

use crate::render::Rect;

/// Compute grid layout for `count` cells within the given outer dimensions.
/// Returns (cols, rows, Vec<Rect>) where each Rect is the content area
/// for one cell (excludes border — border is drawn around it).
pub fn compute_grid(count: usize, outer_w: u16, outer_h: u16) -> (usize, usize, Vec<Rect>) {
    if count == 0 {
        return (0, 0, vec![]);
    }

    let usable_h = outer_h.saturating_sub(2).max(1);
    let usable_w = outer_w.max(1);
    let aspect = usable_w as f32 / usable_h as f32;

    let candidate_layouts = match count {
        1 => vec![(1, 1)],
        2 => {
            if aspect >= 1.2 { vec![(2, 1), (1, 2)] } else { vec![(1, 2), (2, 1)] }
        }
        3 => {
            if aspect >= 1.35 { vec![(3, 1), (2, 2), (1, 3)] }
            else if aspect <= 0.9 { vec![(1, 3), (2, 2), (3, 1)] }
            else { vec![(2, 2), (3, 1), (1, 3)] }
        }
        4 => {
            if aspect <= 0.75 { vec![(1, 4), (2, 2)] } else { vec![(2, 2), (4, 1), (1, 4)] }
        }
        5 => vec![(2, 3), (3, 2), (1, 5), (5, 1)],
        6 => {
            if aspect >= 1.3 { vec![(3, 2), (2, 3)] } else { vec![(2, 3), (3, 2)] }
        }
        _ => {
            let cols = ((count as f64).sqrt().ceil()) as usize;
            let rows = count.div_ceil(cols);
            vec![(cols, rows), (rows, cols)]
        }
    };

    let mut best = candidate_layouts[0];
    let mut best_score = f32::MIN;
    for (cols, rows) in candidate_layouts {
        if cols * rows < count { continue; }
        let cell_outer_w = usable_w as f32 / cols as f32;
        let cell_outer_h = usable_h as f32 / rows as f32;
        let ratio = cell_outer_w / cell_outer_h.max(1.0);
        let closeness = 1.0 / (1.0 + (ratio - 1.8).abs());
        let area = cell_outer_w * cell_outer_h;
        let compactness_penalty = ((cols * rows - count) as f32) * 20.0;
        let score = area + closeness * 120.0 - compactness_penalty;
        if score > best_score {
            best_score = score;
            best = (cols, rows);
        }
    }

    let (cols, rows) = best;
    let mut rects = Vec::with_capacity(count);
    let base_outer_w = usable_w / cols as u16;
    let extra_w = usable_w % cols as u16;
    let base_outer_h = usable_h / rows as u16;
    let extra_h = usable_h % rows as u16;

    let mut y = 1u16;
    for row in 0..rows {
        let row_h = base_outer_h + if row < extra_h as usize { 1 } else { 0 };
        let mut x = 0u16;
        for col in 0..cols {
            let i = row * cols + col;
            if i >= count { break; }
            let col_w = base_outer_w + if col < extra_w as usize { 1 } else { 0 };
            rects.push(Rect {
                x: x + 1,
                y: y + 1,
                width: col_w.saturating_sub(2),
                height: row_h.saturating_sub(2),
            });
            x += col_w;
        }
        y += row_h;
    }

    (cols, rows, rects)
}
