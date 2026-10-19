//! Decoded frame storage and the per-8x8 mode info grid.

use crate::tables::{INTRA_FRAME, NONE};

/// A motion vector in 1/8 sample units.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Mv {
    pub row: i16,
    pub col: i16,
}

impl Mv {
    pub const ZERO: Mv = Mv { row: 0, col: 0 };
    pub fn new(row: i32, col: i32) -> Mv {
        Mv { row: row as i16, col: col as i16 }
    }
}

/// Mode info of one 8x8 block (the arrays of 6.4.4: Skips, TxSizes, MiSizes, YModes, SubModes,
/// RefFrames, InterpFilters, Mvs / SubMvs).
#[derive(Clone, Copy, Debug)]
pub struct MiInfo {
    pub sb_size: u8,
    pub skip: bool,
    pub tx_size: u8,
    pub y_mode: u8,
    pub sub_modes: [u8; 4],
    pub seg_id: u8,
    pub ref_frame: [i8; 2],
    pub interp_filter: u8,
    /// SubMvs[refList][b]; Mvs[refList] is `mv[refList][3]`.
    pub mv: [[Mv; 4]; 2],
}

impl Default for MiInfo {
    fn default() -> Self {
        MiInfo {
            sb_size: 0,
            skip: false,
            tx_size: 0,
            y_mode: 0,
            sub_modes: [0; 4],
            seg_id: 0,
            ref_frame: [INTRA_FRAME, NONE],
            interp_filter: 0,
            mv: [[Mv::ZERO; 4]; 2],
        }
    }
}

/// Mode info of a whole frame (MiRows x MiCols).
#[derive(Clone, Debug, Default)]
pub struct MiGrid {
    pub cols: usize,
    pub rows: usize,
    pub mi: Vec<MiInfo>,
}

impl MiGrid {
    #[inline]
    pub fn at(&self, r: usize, c: usize) -> &MiInfo {
        &self.mi[r * self.cols + c]
    }
}

/// One plane of samples. The allocation covers whole 64x64 superblocks; `width` x `height` is the
/// visible (cropped) area.
#[derive(Clone, Debug, Default)]
pub struct Plane {
    pub data: Vec<u16>,
    pub stride: usize,
    pub width: usize,
    pub height: usize,
}

impl Plane {
    #[inline]
    pub fn row(&self, y: usize) -> &[u16] {
        &self.data[y * self.stride..(y + 1) * self.stride]
    }
}

/// A decoded frame (also used as reference).
#[derive(Clone, Debug)]
pub struct Frame {
    pub planes: [Plane; 3],
    pub width: u32,
    pub height: u32,
    pub ss_x: bool,
    pub ss_y: bool,
    pub bit_depth: u8,
    pub color_space: u8,
    pub color_range: bool,
    pub render_width: u32,
    pub render_height: u32,
    pub key: bool,
    pub intra_only: bool,
}

/// Geometry of the frame planes: allocation sizes for (MiCols, MiRows) rounded to superblocks.
pub fn plane_geometry(width: u32, height: u32, ss_x: bool, ss_y: bool) -> [(usize, usize, usize, usize); 3] {
    let sb_cols = (width as usize).div_ceil(64);
    let sb_rows = (height as usize).div_ceil(64);
    let (aw, ah) = (sb_cols * 64, sb_rows * 64);
    let (sx, sy) = (ss_x as usize, ss_y as usize);
    let cw = (width as usize + sx) >> sx;
    let ch = (height as usize + sy) >> sy;
    [(aw, ah, width as usize, height as usize), (aw >> sx, ah >> sy, cw, ch), (aw >> sx, ah >> sy, cw, ch)]
}

/// Recycled buffers (strip / frame planes, mode info), so that steady-state decoding does not
/// allocate (and page-fault) megabytes per frame.
pub struct Pool<T> {
    bufs: std::sync::Mutex<Vec<Vec<T>>>,
}

impl<T> Default for Pool<T> {
    fn default() -> Self {
        Pool { bufs: Default::default() }
    }
}

impl<T: Clone> Pool<T> {
    const MAX: usize = 32;

    /// A buffer of `len` copies of `fill` (recycled memory when available).
    pub fn take(&self, len: usize, fill: T) -> Vec<T> {
        let found = {
            let mut b = self.bufs.lock().expect("buffer pool");
            b.iter().position(|v| v.capacity() >= len).map(|i| b.swap_remove(i))
        };
        match found {
            Some(mut v) => {
                v.resize(len, fill);
                v
            }
            None => vec![fill; len],
        }
    }

    pub fn put(&self, mut v: Vec<T>) {
        if v.capacity() == 0 {
            return;
        }
        v.clear();
        let mut b = self.bufs.lock().expect("buffer pool");
        if b.len() < Self::MAX {
            b.push(v);
        }
    }
}

/// The decoder's buffer pools.
#[derive(Default)]
pub struct Pools {
    pub samples: Pool<u16>,
    pub mi: Pool<MiInfo>,
}
