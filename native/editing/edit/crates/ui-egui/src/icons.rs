//! Vector icons drawn in code on a 16×16 design grid (crisp at any DPI, recolourable, no
//! third-party or Adobe artwork). Shapes follow NLE conventions (ripple brackets, razor blade…).

use egui::epaint::{PathShape, PathStroke};
use egui::{Color32, Painter, Pos2, Rect, Stroke, pos2, vec2};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Icon {
    Selection,
    TrackSelectFwd,
    TrackSelectBack,
    Ripple,
    Rolling,
    RateStretch,
    Remix,
    Razor,
    Slip,
    Slide,
    Pen,
    Rectangle,
    Ellipse,
    Hand,
    Zoom,
    Type,
    Play,
    Pause,
    StepBack,
    StepFwd,
    GoToIn,
    GoToOut,
    MarkIn,
    MarkOut,
    Marker,
    Insert,
    Overwrite,
    Lift,
    Extract,
    Camera,
    Loop,
    Wrench,
    Plus,
    Eye,
    EyeOff,
    Speaker,
    Mute,
    Lock,
    Unlock,
    SyncLock,
    Mic,
    Folder,
    Film,
    Sequence,
    Audio,
    Image,
    Search,
    ListView,
    IconView,
    Freeform,
    NewItem,
    Trash,
    Home,
    Workspaces,
    Hamburger,
    ChevronDown,
    ChevronRight,
    Magnet,
    Link,
    Keyframe,
    Stopwatch,
    Fx,
    Reset,
    Close,
    Fullscreen,
    Export,
    Gear,
    Info,
    Captions,
    Adjust,
    Nest,
    Undo,
    Redo,
    Bell,
    /// Community chat (the ArtCraft Discord): a speech bubble with three dots. Our own drawing,
    /// not the Discord logo.
    Chat,
    /// Website: a globe.
    Globe,
    /// Source code (GitHub repository): angle brackets and a slash.
    Code,
    Sparkle,
    Grid,
    Square,
    /// Toggle Proxies: a small frame inside a large one.
    Proxy,
    /// Offline media: a broken link.
    Offline,
    /// Mask tracking: backward continuously / one frame, forward one frame / continuously.
    TrackMaskBack,
    TrackMaskBackFrame,
    TrackMaskFwdFrame,
    TrackMaskFwd,
    /// Project panel footer: Sort Icons (lines of decreasing length).
    SortIcons,
    /// Project panel footer: Automate to Sequence (three clips in a row).
    Automate,
    /// Media Browser: a favourite (five-pointed star).
    Star,
    /// Media Browser: back / forward / up one level.
    ChevronLeft,
    ArrowUp,
    /// Media Browser: a local drive and a network location.
    Drive,
    Network,
    /// Media Browser: recent directories (a clock face).
    Clock,
}

pub struct Pen16<'a> {
    painter: &'a Painter,
    rect: Rect,
    color: Color32,
    width: f32,
}

impl Pen16<'_> {
    fn p(&self, x: f32, y: f32) -> Pos2 {
        let s = self.rect.width().min(self.rect.height()) / 16.0;
        let o = self.rect.center() - vec2(8.0 * s, 8.0 * s);
        pos2(o.x + x * s, o.y + y * s)
    }
    fn s(&self) -> f32 {
        self.rect.width().min(self.rect.height()) / 16.0
    }
    fn line(&self, pts: &[(f32, f32)]) {
        let v: Vec<Pos2> = pts.iter().map(|(x, y)| self.p(*x, *y)).collect();
        self.painter.add(PathShape::line(v, PathStroke::new(self.width * self.s(), self.color)));
    }
    fn closed(&self, pts: &[(f32, f32)]) {
        let v: Vec<Pos2> = pts.iter().map(|(x, y)| self.p(*x, *y)).collect();
        self.painter.add(PathShape::closed_line(v, PathStroke::new(self.width * self.s(), self.color)));
    }
    fn fill(&self, pts: &[(f32, f32)]) {
        let v: Vec<Pos2> = pts.iter().map(|(x, y)| self.p(*x, *y)).collect();
        self.painter.add(PathShape::convex_polygon(v, self.color, Stroke::NONE));
    }
    fn circle(&self, x: f32, y: f32, r: f32) {
        self.painter.circle_stroke(self.p(x, y), r * self.s(), Stroke::new(self.width * self.s(), self.color));
    }
    fn dot(&self, x: f32, y: f32, r: f32) {
        self.painter.circle_filled(self.p(x, y), r * self.s(), self.color);
    }
    fn rect(&self, x0: f32, y0: f32, x1: f32, y1: f32) {
        let r = Rect::from_min_max(self.p(x0, y0), self.p(x1, y1));
        self.painter.rect_stroke(r, 1.0 * self.s(), Stroke::new(self.width * self.s(), self.color), egui::StrokeKind::Middle);
    }
    fn rect_fill(&self, x0: f32, y0: f32, x1: f32, y1: f32) {
        let r = Rect::from_min_max(self.p(x0, y0), self.p(x1, y1));
        self.painter.rect_filled(r, 0.8 * self.s(), self.color);
    }
    fn arc(&self, cx: f32, cy: f32, r: f32, a0: f32, a1: f32) {
        let n = 20;
        let pts: Vec<(f32, f32)> = (0..=n)
            .map(|i| {
                let a = (a0 + (a1 - a0) * i as f32 / n as f32).to_radians();
                (cx + r * a.cos(), cy + r * a.sin())
            })
            .collect();
        self.line(&pts);
    }
}

/// Paint `icon` centred in `rect`.
pub fn paint(painter: &Painter, rect: Rect, icon: Icon, color: Color32) {
    let pen = Pen16 { painter, rect, color, width: 1.25 };
    use Icon::*;
    match icon {
        Selection => pen.fill(&[(4.0, 2.0), (4.0, 13.0), (6.8, 10.4), (8.8, 14.6), (10.4, 13.9), (8.5, 9.8), (12.2, 9.6)]),
        TrackSelectFwd => {
            pen.fill(&[(2.0, 3.0), (2.0, 11.0), (4.0, 9.2), (5.4, 12.2), (6.6, 11.6), (5.3, 8.7), (7.8, 8.5)]);
            pen.fill(&[(8.5, 4.5), (11.0, 7.0), (8.5, 9.5)]);
            pen.fill(&[(11.5, 4.5), (14.0, 7.0), (11.5, 9.5)]);
        }
        TrackSelectBack => {
            pen.fill(&[(14.0, 3.0), (14.0, 11.0), (12.0, 9.2), (10.6, 12.2), (9.4, 11.6), (10.7, 8.7), (8.2, 8.5)]);
            pen.fill(&[(7.5, 4.5), (5.0, 7.0), (7.5, 9.5)]);
            pen.fill(&[(4.5, 4.5), (2.0, 7.0), (4.5, 9.5)]);
        }
        Ripple => {
            pen.line(&[(9.0, 2.5), (6.5, 2.5), (6.5, 13.5), (9.0, 13.5)]);
            pen.fill(&[(10.0, 5.0), (13.5, 8.0), (10.0, 11.0)]);
            pen.line(&[(2.0, 8.0), (6.5, 8.0)]);
        }
        Rolling => {
            pen.line(&[(6.0, 2.5), (8.0, 2.5), (8.0, 13.5), (6.0, 13.5)]);
            pen.line(&[(10.0, 2.5), (8.0, 2.5)]);
            pen.line(&[(10.0, 13.5), (8.0, 13.5)]);
            pen.fill(&[(5.0, 5.5), (2.0, 8.0), (5.0, 10.5)]);
            pen.fill(&[(11.0, 5.5), (14.0, 8.0), (11.0, 10.5)]);
        }
        RateStretch => {
            pen.line(&[(2.5, 3.0), (2.5, 13.0)]);
            pen.line(&[(13.5, 3.0), (13.5, 13.0)]);
            pen.line(&[(4.5, 8.0), (11.5, 8.0)]);
            pen.fill(&[(4.0, 8.0), (6.5, 6.0), (6.5, 10.0)]);
            pen.fill(&[(12.0, 8.0), (9.5, 6.0), (9.5, 10.0)]);
            pen.circle(8.0, 4.0, 1.4);
            pen.circle(8.0, 12.0, 1.4);
        }
        Remix => {
            // original: a waveform cut in three blocks that swap places (two arrows over a gap)
            pen.line(&[(2.0, 8.0), (3.0, 5.5), (4.0, 10.5), (5.0, 7.0)]);
            pen.line(&[(11.0, 7.0), (12.0, 10.5), (13.0, 5.5), (14.0, 8.0)]);
            pen.line(&[(7.0, 3.0), (7.0, 13.0)]);
            pen.line(&[(9.0, 3.0), (9.0, 13.0)]);
            pen.line(&[(4.0, 2.5), (12.0, 2.5)]);
            pen.fill(&[(12.5, 2.5), (10.5, 1.0), (10.5, 4.0)]);
            pen.line(&[(12.0, 13.5), (4.0, 13.5)]);
            pen.fill(&[(3.5, 13.5), (5.5, 12.0), (5.5, 14.8)]);
        }
        Razor => {
            pen.closed(&[(2.5, 5.0), (13.5, 5.0), (13.5, 11.0), (2.5, 11.0)]);
            pen.circle(8.0, 8.0, 1.3);
            pen.line(&[(5.0, 8.0), (6.5, 8.0)]);
            pen.line(&[(9.5, 8.0), (11.0, 8.0)]);
        }
        Slip => {
            pen.line(&[(2.5, 4.0), (2.5, 12.0)]);
            pen.line(&[(13.5, 4.0), (13.5, 12.0)]);
            pen.fill(&[(4.5, 8.0), (7.0, 6.0), (7.0, 10.0)]);
            pen.fill(&[(11.5, 8.0), (9.0, 6.0), (9.0, 10.0)]);
            pen.line(&[(5.5, 3.0), (10.5, 3.0)]);
            pen.line(&[(5.5, 13.0), (10.5, 13.0)]);
        }
        Slide => {
            pen.line(&[(5.5, 3.0), (5.5, 13.0)]);
            pen.line(&[(10.5, 3.0), (10.5, 13.0)]);
            pen.fill(&[(1.5, 8.0), (4.0, 6.0), (4.0, 10.0)]);
            pen.fill(&[(14.5, 8.0), (12.0, 6.0), (12.0, 10.0)]);
        }
        Pen => {
            pen.closed(&[(8.0, 2.0), (12.0, 9.0), (9.5, 13.5), (6.5, 13.5), (4.0, 9.0)]);
            pen.line(&[(8.0, 2.0), (8.0, 8.0)]);
            pen.dot(8.0, 9.0, 1.0);
        }
        Rectangle => pen.rect(2.5, 4.0, 13.5, 12.0),
        Ellipse => pen.circle(8.0, 8.0, 5.5),
        Hand => {
            pen.line(&[(5.0, 8.5), (5.0, 4.0), (6.5, 3.2), (7.3, 4.0), (7.3, 7.5)]);
            pen.line(&[(7.3, 4.0), (7.3, 2.8), (8.8, 2.2), (9.6, 3.0), (9.6, 7.5)]);
            pen.line(&[(9.6, 3.6), (11.0, 3.2), (11.9, 4.0), (11.9, 8.0)]);
            pen.line(&[(11.9, 5.5), (13.0, 5.3), (13.6, 6.0), (13.6, 10.0), (11.5, 14.0), (6.5, 14.0), (3.0, 10.0), (2.5, 8.5), (3.5, 7.8), (5.0, 8.5)]);
        }
        Zoom => {
            pen.circle(6.8, 6.8, 4.3);
            pen.line(&[(10.0, 10.0), (14.0, 14.0)]);
            pen.line(&[(4.8, 6.8), (8.8, 6.8)]);
            pen.line(&[(6.8, 4.8), (6.8, 8.8)]);
        }
        Type => {
            pen.line(&[(3.0, 4.5), (3.0, 2.8), (13.0, 2.8), (13.0, 4.5)]);
            pen.line(&[(8.0, 2.8), (8.0, 13.5)]);
            pen.line(&[(6.0, 13.5), (10.0, 13.5)]);
        }
        Play => pen.fill(&[(4.5, 2.8), (13.0, 8.0), (4.5, 13.2)]),
        Pause => {
            pen.rect_fill(4.0, 3.0, 6.8, 13.0);
            pen.rect_fill(9.2, 3.0, 12.0, 13.0);
        }
        StepBack => {
            pen.rect_fill(3.5, 3.5, 5.0, 12.5);
            pen.fill(&[(12.5, 3.5), (6.0, 8.0), (12.5, 12.5)]);
        }
        StepFwd => {
            pen.rect_fill(11.0, 3.5, 12.5, 12.5);
            pen.fill(&[(3.5, 3.5), (10.0, 8.0), (3.5, 12.5)]);
        }
        GoToIn => {
            pen.line(&[(5.0, 3.0), (3.0, 3.0), (3.0, 13.0), (5.0, 13.0)]);
            pen.fill(&[(13.0, 3.5), (6.0, 8.0), (13.0, 12.5)]);
        }
        GoToOut => {
            pen.line(&[(11.0, 3.0), (13.0, 3.0), (13.0, 13.0), (11.0, 13.0)]);
            pen.fill(&[(3.0, 3.5), (10.0, 8.0), (3.0, 12.5)]);
        }
        MarkIn => pen.line(&[(10.5, 2.5), (5.5, 2.5), (5.5, 13.5), (10.5, 13.5)]),
        MarkOut => pen.line(&[(5.5, 2.5), (10.5, 2.5), (10.5, 13.5), (5.5, 13.5)]),
        Marker => pen.fill(&[(4.0, 2.5), (12.0, 2.5), (12.0, 9.5), (8.0, 13.5), (4.0, 9.5)]),
        Insert => {
            pen.rect(2.0, 5.0, 14.0, 13.0);
            pen.fill(&[(5.5, 1.5), (10.5, 1.5), (8.0, 5.5)]);
            pen.line(&[(8.0, 5.0), (8.0, 13.0)]);
        }
        Overwrite => {
            pen.rect(2.0, 7.0, 14.0, 13.0);
            pen.rect_fill(5.0, 8.5, 11.0, 11.5);
            pen.fill(&[(5.5, 1.5), (10.5, 1.5), (8.0, 5.5)]);
        }
        Lift => {
            pen.rect(2.0, 8.0, 14.0, 13.5);
            pen.line(&[(8.0, 11.0), (8.0, 2.5)]);
            pen.line(&[(5.0, 5.5), (8.0, 2.5), (11.0, 5.5)]);
        }
        Extract => {
            pen.rect(2.0, 8.0, 14.0, 13.5);
            pen.line(&[(8.0, 11.0), (8.0, 2.5)]);
            pen.line(&[(5.0, 5.5), (8.0, 2.5), (11.0, 5.5)]);
            pen.line(&[(5.5, 10.75), (6.5, 10.75)]);
            pen.line(&[(9.5, 10.75), (10.5, 10.75)]);
        }
        Camera => {
            pen.closed(&[(2.0, 5.0), (5.0, 5.0), (6.0, 3.5), (10.0, 3.5), (11.0, 5.0), (14.0, 5.0), (14.0, 12.5), (2.0, 12.5)]);
            pen.circle(8.0, 8.7, 2.3);
        }
        Loop => {
            pen.arc(8.0, 8.0, 5.0, 200.0, 520.0);
            pen.fill(&[(1.8, 5.5), (5.2, 5.5), (3.2, 8.5)]);
        }
        Wrench => {
            pen.line(&[(3.0, 13.0), (9.0, 7.0)]);
            pen.arc(10.5, 5.5, 3.0, 110.0, 400.0);
        }
        Plus => {
            pen.line(&[(8.0, 3.0), (8.0, 13.0)]);
            pen.line(&[(3.0, 8.0), (13.0, 8.0)]);
        }
        Eye | EyeOff => {
            pen.line(&[(1.5, 8.0), (4.0, 5.0), (8.0, 3.8), (12.0, 5.0), (14.5, 8.0), (12.0, 11.0), (8.0, 12.2), (4.0, 11.0), (1.5, 8.0)]);
            pen.circle(8.0, 8.0, 2.0);
            if icon == EyeOff {
                pen.line(&[(2.5, 13.5), (13.5, 2.5)]);
            }
        }
        Speaker | Mute => {
            pen.fill(&[(2.0, 6.0), (5.0, 6.0), (9.0, 2.5), (9.0, 13.5), (5.0, 10.0), (2.0, 10.0)]);
            if icon == Mute {
                pen.line(&[(10.5, 6.0), (14.0, 10.0)]);
                pen.line(&[(14.0, 6.0), (10.5, 10.0)]);
            } else {
                pen.arc(9.0, 8.0, 3.0, -50.0, 50.0);
                pen.arc(9.0, 8.0, 5.5, -50.0, 50.0);
            }
        }
        Lock | Unlock => {
            pen.rect(3.5, 7.0, 12.5, 14.0);
            if icon == Lock {
                pen.arc(8.0, 7.0, 3.0, 180.0, 360.0);
            } else {
                pen.arc(8.0, 5.0, 3.0, 180.0, 330.0);
            }
            pen.dot(8.0, 10.5, 1.0);
        }
        SyncLock => {
            pen.rect(3.0, 7.0, 13.0, 14.0);
            pen.arc(8.0, 7.0, 3.0, 180.0, 360.0);
            pen.line(&[(5.5, 10.5), (7.0, 12.0), (10.5, 9.0)]);
        }
        Mic => {
            pen.rect(6.0, 2.0, 10.0, 10.0);
            pen.arc(8.0, 8.0, 4.5, 0.0, 180.0);
            pen.line(&[(8.0, 12.5), (8.0, 14.5)]);
        }
        Folder => pen.closed(&[(1.5, 4.0), (6.0, 4.0), (7.5, 5.5), (14.5, 5.5), (14.5, 13.0), (1.5, 13.0)]),
        Film => {
            pen.rect(2.0, 3.0, 14.0, 13.0);
            for y in [5.0, 8.0, 11.0] {
                pen.dot(3.8, y, 0.6);
                pen.dot(12.2, y, 0.6);
            }
            pen.line(&[(5.5, 3.0), (5.5, 13.0)]);
            pen.line(&[(10.5, 3.0), (10.5, 13.0)]);
        }
        Sequence => {
            pen.rect(1.5, 3.5, 14.5, 12.5);
            pen.rect_fill(3.0, 5.5, 9.0, 7.5);
            pen.rect_fill(6.0, 9.0, 13.0, 11.0);
        }
        Audio => {
            for (i, h) in [3.0, 6.0, 9.0, 5.0, 7.0, 3.0].iter().enumerate() {
                let x = 2.5 + i as f32 * 2.2;
                pen.line(&[(x, 8.0 - h / 2.0), (x, 8.0 + h / 2.0)]);
            }
        }
        Image => {
            pen.rect(2.0, 3.0, 14.0, 13.0);
            pen.line(&[(2.5, 12.0), (6.0, 8.0), (9.0, 11.0), (11.0, 9.0), (13.5, 12.0)]);
            pen.dot(10.5, 6.0, 1.1);
        }
        Search => {
            pen.circle(6.8, 6.8, 4.3);
            pen.line(&[(10.0, 10.0), (14.0, 14.0)]);
        }
        ListView => {
            for y in [4.0, 8.0, 12.0] {
                pen.dot(3.0, y, 0.8);
                pen.line(&[(5.5, y), (14.0, y)]);
            }
        }
        IconView => {
            pen.rect(2.5, 2.5, 7.0, 7.0);
            pen.rect(9.0, 2.5, 13.5, 7.0);
            pen.rect(2.5, 9.0, 7.0, 13.5);
            pen.rect(9.0, 9.0, 13.5, 13.5);
        }
        Freeform => {
            pen.rect(2.0, 3.0, 8.0, 8.0);
            pen.rect(7.0, 9.0, 14.0, 13.5);
            pen.rect(10.0, 2.0, 14.0, 6.0);
        }
        NewItem => {
            pen.closed(&[(3.0, 2.0), (10.0, 2.0), (13.0, 5.0), (13.0, 14.0), (3.0, 14.0)]);
            pen.line(&[(8.0, 6.0), (8.0, 11.0)]);
            pen.line(&[(5.5, 8.5), (10.5, 8.5)]);
        }
        Trash => {
            pen.line(&[(2.5, 4.0), (13.5, 4.0)]);
            pen.line(&[(6.0, 4.0), (6.5, 2.5), (9.5, 2.5), (10.0, 4.0)]);
            pen.closed(&[(3.8, 4.0), (12.2, 4.0), (11.4, 14.0), (4.6, 14.0)]);
        }
        Home => {
            pen.line(&[(1.5, 8.0), (8.0, 2.0), (14.5, 8.0)]);
            pen.line(&[(3.5, 6.5), (3.5, 14.0), (12.5, 14.0), (12.5, 6.5)]);
            pen.line(&[(6.5, 14.0), (6.5, 10.0), (9.5, 10.0), (9.5, 14.0)]);
        }
        Workspaces => {
            pen.rect(1.5, 2.5, 14.5, 13.5);
            pen.line(&[(6.0, 2.5), (6.0, 13.5)]);
            pen.line(&[(6.0, 8.0), (14.5, 8.0)]);
        }
        Hamburger => {
            for y in [4.5, 8.0, 11.5] {
                pen.line(&[(3.0, y), (13.0, y)]);
            }
        }
        ChevronDown => pen.line(&[(4.0, 6.0), (8.0, 10.0), (12.0, 6.0)]),
        ChevronRight => pen.line(&[(6.0, 4.0), (10.0, 8.0), (6.0, 12.0)]),
        Magnet => {
            pen.arc(8.0, 8.0, 4.5, 0.0, 180.0);
            pen.line(&[(3.5, 8.0), (3.5, 2.5)]);
            pen.line(&[(12.5, 8.0), (12.5, 2.5)]);
            pen.rect_fill(2.5, 2.5, 4.8, 4.8);
            pen.rect_fill(11.2, 2.5, 13.5, 4.8);
        }
        Link => {
            pen.closed(&[(2.0, 6.0), (8.0, 6.0), (8.0, 10.0), (2.0, 10.0)]);
            pen.closed(&[(8.0, 6.0), (14.0, 6.0), (14.0, 10.0), (8.0, 10.0)]);
        }
        Keyframe => pen.fill(&[(8.0, 3.0), (13.0, 8.0), (8.0, 13.0), (3.0, 8.0)]),
        Stopwatch => {
            pen.circle(8.0, 9.0, 5.0);
            pen.line(&[(8.0, 9.0), (8.0, 6.0)]);
            pen.line(&[(6.5, 2.0), (9.5, 2.0)]);
            pen.line(&[(8.0, 2.0), (8.0, 4.0)]);
        }
        Fx => {
            pen.line(&[(7.5, 2.5), (6.0, 2.5), (5.0, 4.0), (4.0, 13.5)]);
            pen.line(&[(2.5, 6.5), (7.0, 6.5)]);
            pen.line(&[(8.5, 7.0), (13.5, 13.5)]);
            pen.line(&[(13.5, 7.0), (8.5, 13.5)]);
        }
        Reset => {
            pen.arc(8.0, 8.5, 5.0, 200.0, 500.0);
            pen.fill(&[(1.5, 6.0), (5.5, 5.5), (3.0, 9.0)]);
        }
        Close => {
            pen.line(&[(4.0, 4.0), (12.0, 12.0)]);
            pen.line(&[(12.0, 4.0), (4.0, 12.0)]);
        }
        Fullscreen => {
            pen.line(&[(2.5, 6.0), (2.5, 2.5), (6.0, 2.5)]);
            pen.line(&[(10.0, 2.5), (13.5, 2.5), (13.5, 6.0)]);
            pen.line(&[(13.5, 10.0), (13.5, 13.5), (10.0, 13.5)]);
            pen.line(&[(6.0, 13.5), (2.5, 13.5), (2.5, 10.0)]);
        }
        Export => {
            pen.line(&[(8.0, 10.0), (8.0, 2.0)]);
            pen.line(&[(5.0, 5.0), (8.0, 2.0), (11.0, 5.0)]);
            pen.line(&[(3.0, 8.0), (3.0, 14.0), (13.0, 14.0), (13.0, 8.0)]);
        }
        Gear => {
            pen.circle(8.0, 8.0, 2.2);
            for i in 0..8 {
                let a = (i as f32 * 45.0).to_radians();
                pen.line(&[(8.0 + 4.0 * a.cos(), 8.0 + 4.0 * a.sin()), (8.0 + 6.0 * a.cos(), 8.0 + 6.0 * a.sin())]);
            }
            pen.circle(8.0, 8.0, 4.2);
        }
        Info => {
            pen.circle(8.0, 8.0, 6.0);
            pen.line(&[(8.0, 7.0), (8.0, 11.5)]);
            pen.dot(8.0, 4.8, 0.9);
        }
        Captions => {
            pen.rect(1.5, 3.0, 14.5, 13.0);
            pen.line(&[(4.0, 9.0), (7.0, 9.0)]);
            pen.line(&[(9.0, 9.0), (12.0, 9.0)]);
            pen.line(&[(4.0, 11.0), (12.0, 11.0)]);
        }
        Adjust => {
            for (y, x) in [(4.0, 10.0), (8.0, 5.0), (12.0, 9.0)] {
                pen.line(&[(2.0, y), (14.0, y)]);
                pen.dot(x, y, 1.5);
            }
        }
        Nest => {
            pen.rect(1.5, 2.5, 14.5, 13.5);
            pen.rect(4.5, 5.5, 11.5, 10.5);
        }
        Undo => {
            pen.arc(9.0, 9.0, 4.5, 180.0, 450.0);
            pen.fill(&[(2.0, 9.0), (4.5, 6.0), (7.0, 9.0)]);
        }
        Redo => {
            pen.arc(7.0, 9.0, 4.5, 90.0, 360.0);
            pen.fill(&[(14.0, 9.0), (11.5, 6.0), (9.0, 9.0)]);
        }
        Bell => {
            pen.line(&[(3.0, 12.0), (13.0, 12.0)]);
            pen.line(&[(4.5, 12.0), (4.5, 7.0)]);
            pen.line(&[(11.5, 12.0), (11.5, 7.0)]);
            pen.arc(8.0, 7.0, 3.5, 180.0, 360.0);
            pen.dot(8.0, 13.8, 1.0);
        }
        Chat => {
            pen.line(&[
                (3.5, 3.0),
                (12.5, 3.0),
                (14.0, 4.5),
                (14.0, 9.5),
                (12.5, 11.0),
                (7.0, 11.0),
                (4.0, 14.0),
                (4.5, 11.0),
                (3.5, 11.0),
                (2.0, 9.5),
                (2.0, 4.5),
                (3.5, 3.0),
            ]);
            pen.dot(5.5, 7.0, 0.9);
            pen.dot(8.0, 7.0, 0.9);
            pen.dot(10.5, 7.0, 0.9);
        }
        Globe => {
            pen.circle(8.0, 8.0, 6.0);
            pen.line(&[(2.0, 8.0), (14.0, 8.0)]);
            pen.line(&[(3.0, 5.0), (13.0, 5.0)]);
            pen.line(&[(3.0, 11.0), (13.0, 11.0)]);
            pen.line(&[(8.0, 2.0), (6.0, 5.0), (5.5, 8.0), (6.0, 11.0), (8.0, 14.0)]);
            pen.line(&[(8.0, 2.0), (10.0, 5.0), (10.5, 8.0), (10.0, 11.0), (8.0, 14.0)]);
        }
        Code => {
            pen.line(&[(5.0, 4.0), (1.5, 8.0), (5.0, 12.0)]);
            pen.line(&[(11.0, 4.0), (14.5, 8.0), (11.0, 12.0)]);
            pen.line(&[(9.5, 2.5), (6.5, 13.5)]);
        }
        Sparkle => {
            pen.fill(&[(8.0, 1.5), (9.5, 6.5), (14.5, 8.0), (9.5, 9.5), (8.0, 14.5), (6.5, 9.5), (1.5, 8.0), (6.5, 6.5)]);
        }
        Grid => {
            pen.rect(2.0, 2.0, 14.0, 14.0);
            pen.line(&[(6.0, 2.0), (6.0, 14.0)]);
            pen.line(&[(10.0, 2.0), (10.0, 14.0)]);
            pen.line(&[(2.0, 6.0), (14.0, 6.0)]);
            pen.line(&[(2.0, 10.0), (14.0, 10.0)]);
        }
        Square => pen.rect(3.0, 3.0, 13.0, 13.0),
        Proxy => {
            pen.rect(1.5, 3.0, 14.5, 13.0);
            pen.rect_fill(3.5, 8.0, 8.5, 11.5);
            pen.line(&[(9.5, 7.0), (12.5, 4.5)]);
            pen.line(&[(10.5, 4.5), (12.5, 4.5), (12.5, 6.5)]);
        }
        Offline => {
            pen.line(&[(7.0, 4.0), (4.5, 4.0), (2.5, 6.0), (2.5, 7.5), (4.0, 9.0)]);
            pen.line(&[(9.0, 12.0), (11.5, 12.0), (13.5, 10.0), (13.5, 8.5), (12.0, 7.0)]);
            pen.line(&[(5.5, 2.0), (6.5, 0.8)]);
            pen.line(&[(10.5, 14.0), (9.5, 15.2)]);
            pen.line(&[(3.0, 1.5), (3.5, 2.8)]);
            pen.line(&[(13.0, 14.5), (12.5, 13.2)]);
        }
        TrackMaskBack => {
            pen.fill(&[(8.0, 3.5), (2.0, 8.0), (8.0, 12.5)]);
            pen.fill(&[(14.0, 3.5), (8.0, 8.0), (14.0, 12.5)]);
        }
        TrackMaskBackFrame => pen.fill(&[(11.0, 3.5), (4.0, 8.0), (11.0, 12.5)]),
        TrackMaskFwdFrame => pen.fill(&[(5.0, 3.5), (12.0, 8.0), (5.0, 12.5)]),
        TrackMaskFwd => {
            pen.fill(&[(2.0, 3.5), (8.0, 8.0), (2.0, 12.5)]);
            pen.fill(&[(8.0, 3.5), (14.0, 8.0), (8.0, 12.5)]);
        }
        SortIcons => {
            for (i, w) in [12.0, 9.5, 7.0, 4.5].iter().enumerate() {
                let y = 3.5 + i as f32 * 3.0;
                pen.line(&[(2.0, y), (2.0 + w, y)]);
            }
        }
        Automate => {
            pen.rect_fill(1.5, 5.0, 4.5, 11.0);
            pen.rect_fill(5.5, 5.0, 8.5, 11.0);
            pen.rect_fill(9.5, 5.0, 12.0, 11.0);
            pen.line(&[(13.5, 5.0), (13.5, 11.0)]);
        }
        Star => {
            let pts: Vec<(f32, f32)> = (0..10)
                .map(|i| {
                    let a = (-90.0 + i as f32 * 36.0f32).to_radians();
                    let r = if i % 2 == 0 { 6.5 } else { 2.7 };
                    (8.0 + r * a.cos(), 8.5 + r * a.sin())
                })
                .collect();
            pen.closed(&pts);
        }
        ChevronLeft => pen.line(&[(10.0, 4.0), (6.0, 8.0), (10.0, 12.0)]),
        ArrowUp => {
            pen.line(&[(8.0, 13.5), (8.0, 3.0)]);
            pen.line(&[(4.0, 7.0), (8.0, 3.0), (12.0, 7.0)]);
        }
        Drive => {
            pen.rect(1.5, 5.0, 14.5, 11.5);
            pen.dot(12.0, 8.25, 0.8);
            pen.line(&[(3.5, 8.25), (8.0, 8.25)]);
        }
        Network => {
            pen.circle(8.0, 8.0, 6.0);
            pen.line(&[(2.0, 8.0), (14.0, 8.0)]);
            pen.arc(8.0, 8.0, 6.0, -90.0, 90.0);
            pen.line(&[(8.0, 2.0), (8.0, 14.0)]);
        }
        Clock => {
            pen.circle(8.0, 8.0, 6.0);
            pen.line(&[(8.0, 4.5), (8.0, 8.0), (10.5, 9.5)]);
        }
    }
}

/// An icon button: returns the response; `active` draws the selected state.
pub fn button(ui: &mut egui::Ui, icon: Icon, size: f32, active: bool, t: &crate::theme::Tokens, tooltip: &str) -> egui::Response {
    let (rect, resp) = ui.allocate_exact_size(vec2(size, size), egui::Sense::click());
    let bg = if resp.is_pointer_button_down_on() {
        Some(t.pressed)
    } else if resp.hovered() {
        Some(t.hover)
    } else {
        None
    };
    if let Some(bg) = bg {
        ui.painter().rect_filled(rect, t.radius_sm, bg);
    }
    let col = if active {
        t.icon_active
    } else if resp.hovered() {
        t.tab_text_active
    } else {
        t.icon
    };
    paint(ui.painter(), rect.shrink(size * 0.2), icon, col);
    if tooltip.is_empty() { resp } else { resp.on_hover_text(tooltip) }
}
