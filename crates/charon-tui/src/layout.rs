//! Manual split-layout engine for the session grid.
//!
//! A binary layout tree of panes: a leaf holds a pane id; a split divides its
//! area horizontally (side by side) or vertically (stacked) at a ratio. This
//! module is pure (no rendering/IO) so the geometry is unit-testable; the TUI
//! converts [`Rect`] to its render rect and drives split/resize from keys+mouse.
#![allow(dead_code)]

/// A rectangle in terminal cells (mirrors `render::Rect`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rect {
    pub x: u16,
    pub y: u16,
    pub width: u16,
    pub height: u16,
}

/// Split orientation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Dir {
    /// Children side by side (divide width); `a` = left, `b` = right.
    Horizontal,
    /// Children stacked (divide height); `a` = top, `b` = bottom.
    Vertical,
}

/// A node in the layout tree.
#[derive(Clone, Debug, PartialEq)]
pub enum Node {
    Leaf(u64),
    Split {
        dir: Dir,
        /// Fraction of the area given to `a` (0.05..=0.95).
        ratio: f32,
        a: Box<Node>,
        b: Box<Node>,
    },
}

impl Node {
    /// All pane ids, left-to-right / top-to-bottom.
    pub fn leaves(&self) -> Vec<u64> {
        let mut out = Vec::new();
        self.collect_leaves(&mut out);
        out
    }

    fn collect_leaves(&self, out: &mut Vec<u64>) {
        match self {
            Node::Leaf(id) => out.push(*id),
            Node::Split { a, b, .. } => {
                a.collect_leaves(out);
                b.collect_leaves(out);
            }
        }
    }

    pub fn contains(&self, pane: u64) -> bool {
        self.leaves().contains(&pane)
    }

    /// Replace the leaf `target` with a split of `target` and `new_pane`.
    /// `ratio` is the fraction kept by `target`. No-op if `target` isn't present.
    pub fn split(self, target: u64, new_pane: u64, dir: Dir, ratio: f32) -> Node {
        let ratio = ratio.clamp(0.05, 0.95);
        match self {
            Node::Leaf(id) if id == target => Node::Split {
                dir,
                ratio,
                a: Box::new(Node::Leaf(target)),
                b: Box::new(Node::Leaf(new_pane)),
            },
            leaf @ Node::Leaf(_) => leaf,
            Node::Split { dir: d, ratio: r, a, b } => Node::Split {
                dir: d,
                ratio: r,
                a: Box::new(a.split(target, new_pane, dir, ratio)),
                b: Box::new(b.split(target, new_pane, dir, ratio)),
            },
        }
    }

    /// Remove `pane`; the sibling collapses up to take the split's place.
    /// Returns `None` if the tree becomes empty (removed the last leaf).
    pub fn remove(self, pane: u64) -> Option<Node> {
        match self {
            Node::Leaf(id) if id == pane => None,
            leaf @ Node::Leaf(_) => Some(leaf),
            Node::Split { dir, ratio, a, b } => match (a.remove(pane), b.remove(pane)) {
                (Some(a), Some(b)) => Some(Node::Split {
                    dir,
                    ratio,
                    a: Box::new(a),
                    b: Box::new(b),
                }),
                (Some(n), None) | (None, Some(n)) => Some(n),
                (None, None) => None,
            },
        }
    }

    /// Nudge the ratio of the split whose `a`-side contains `pane` by `delta`.
    /// Lets the focused pane grow/shrink against its neighbor.
    pub fn resize(&mut self, pane: u64, delta: f32) -> bool {
        if let Node::Split { ratio, a, b, .. } = self {
            if a.contains(pane) && !matches!(**a, Node::Split { .. }) {
                *ratio = (*ratio + delta).clamp(0.05, 0.95);
                return true;
            }
            if b.contains(pane) && !matches!(**b, Node::Split { .. }) {
                *ratio = (*ratio - delta).clamp(0.05, 0.95);
                return true;
            }
            return a.resize(pane, delta) || b.resize(pane, delta);
        }
        false
    }

    /// Compute the rect for every pane within `area`, leaving a `gap` cell
    /// gutter between split children (for borders).
    pub fn compute(&self, area: Rect, gap: u16) -> Vec<(u64, Rect)> {
        let mut out = Vec::new();
        self.compute_into(area, gap, &mut out);
        out
    }

    fn compute_into(&self, area: Rect, gap: u16, out: &mut Vec<(u64, Rect)>) {
        match self {
            Node::Leaf(id) => out.push((*id, area)),
            Node::Split { dir, ratio, a, b } => {
                let (ra, rb) = split_rect(area, *dir, *ratio, gap);
                a.compute_into(ra, gap, out);
                b.compute_into(rb, gap, out);
            }
        }
    }

    /// Which pane's rect contains the point `(px, py)`, given the same `area`/`gap`.
    pub fn leaf_at(&self, area: Rect, gap: u16, px: u16, py: u16) -> Option<u64> {
        self.compute(area, gap)
            .into_iter()
            .find(|(_, r)| px >= r.x && px < r.x + r.width && py >= r.y && py < r.y + r.height)
            .map(|(id, _)| id)
    }
}

fn split_rect(area: Rect, dir: Dir, ratio: f32, gap: u16) -> (Rect, Rect) {
    match dir {
        Dir::Horizontal => {
            let usable = area.width.saturating_sub(gap);
            let aw = ((usable as f32 * ratio).round() as u16).clamp(1, usable.saturating_sub(1).max(1));
            let a = Rect { x: area.x, y: area.y, width: aw, height: area.height };
            let b = Rect {
                x: area.x + aw + gap,
                y: area.y,
                width: usable.saturating_sub(aw),
                height: area.height,
            };
            (a, b)
        }
        Dir::Vertical => {
            let usable = area.height.saturating_sub(gap);
            let ah = ((usable as f32 * ratio).round() as u16).clamp(1, usable.saturating_sub(1).max(1));
            let a = Rect { x: area.x, y: area.y, width: area.width, height: ah };
            let b = Rect {
                x: area.x,
                y: area.y + ah + gap,
                width: area.width,
                height: usable.saturating_sub(ah),
            };
            (a, b)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const AREA: Rect = Rect { x: 0, y: 0, width: 100, height: 40 };

    #[test]
    fn single_leaf_fills_area() {
        let t = Node::Leaf(1);
        assert_eq!(t.compute(AREA, 0), vec![(1, AREA)]);
        assert_eq!(t.leaves(), vec![1]);
    }

    #[test]
    fn horizontal_split_divides_width() {
        let t = Node::Leaf(1).split(1, 2, Dir::Horizontal, 0.5);
        let rects = t.compute(AREA, 0);
        assert_eq!(rects.len(), 2);
        let (a, b) = (rects[0].1, rects[1].1);
        assert_eq!(a, Rect { x: 0, y: 0, width: 50, height: 40 });
        assert_eq!(b, Rect { x: 50, y: 0, width: 50, height: 40 });
        // Panes tile the area with no overlap and full coverage.
        assert_eq!(a.width + b.width, AREA.width);
    }

    #[test]
    fn vertical_split_with_gap() {
        let t = Node::Leaf(1).split(1, 2, Dir::Vertical, 0.5);
        let rects = t.compute(AREA, 1);
        let (a, b) = (rects[0].1, rects[1].1);
        // gap of 1 between the two stacked panes
        assert_eq!(a.height + b.height, AREA.height - 1);
        assert_eq!(b.y, a.y + a.height + 1);
    }

    #[test]
    fn nested_splits() {
        // split 1 horizontally → [1,2]; then split 2 vertically → [1, [2,3]]
        let t = Node::Leaf(1)
            .split(1, 2, Dir::Horizontal, 0.5)
            .split(2, 3, Dir::Vertical, 0.5);
        assert_eq!(t.leaves(), vec![1, 2, 3]);
        let rects = t.compute(AREA, 0);
        assert_eq!(rects.len(), 3);
        // pane 1 keeps the left half
        assert_eq!(rects.iter().find(|(id, _)| *id == 1).unwrap().1.width, 50);
    }

    #[test]
    fn remove_collapses_sibling() {
        let t = Node::Leaf(1).split(1, 2, Dir::Horizontal, 0.5);
        let t = t.remove(2).unwrap();
        assert_eq!(t, Node::Leaf(1));
        assert_eq!(t.compute(AREA, 0), vec![(1, AREA)]);
        assert_eq!(Node::Leaf(1).remove(1), None);
    }

    #[test]
    fn leaf_at_point() {
        let t = Node::Leaf(1).split(1, 2, Dir::Horizontal, 0.5);
        assert_eq!(t.leaf_at(AREA, 0, 10, 10), Some(1));
        assert_eq!(t.leaf_at(AREA, 0, 80, 10), Some(2));
        assert_eq!(t.leaf_at(AREA, 0, 200, 10), None);
    }

    #[test]
    fn resize_adjusts_neighbor() {
        let mut t = Node::Leaf(1).split(1, 2, Dir::Horizontal, 0.5);
        assert!(t.resize(1, 0.1)); // grow pane 1
        let rects = t.compute(AREA, 0);
        assert_eq!(rects.iter().find(|(id, _)| *id == 1).unwrap().1.width, 60);
        assert!(t.resize(2, 0.1)); // grow pane 2 (shrinks a-side)
        let rects = t.compute(AREA, 0);
        assert_eq!(rects.iter().find(|(id, _)| *id == 1).unwrap().1.width, 50);
    }
}
