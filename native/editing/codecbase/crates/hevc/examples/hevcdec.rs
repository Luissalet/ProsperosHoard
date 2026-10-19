//! Decode an Annex-B H.265 file to raw planar YUV (yuv420p or yuv420p10le):
//! `cargo run --release -p lumiere-codecbase-hevc --example hevcdec -- in.hevc out.yuv`

use lumiere_codecbase_hevc::Decoder;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: hevcdec <in.hevc> [out.yuv]");
        std::process::exit(2);
    }
    let data = std::fs::read(&args[1]).expect("read input");
    let t0 = std::time::Instant::now();
    let mut dec = Decoder::new();
    let mut pics = dec.decode(&data, 0).expect("decode");
    pics.extend(dec.flush());
    let secs = t0.elapsed().as_secs_f64();
    eprintln!("{} pictures in {:.3}s ({:.1} fps)", pics.len(), secs, pics.len() as f64 / secs);
    if let Some(out) = args.get(2) {
        let mut buf = Vec::new();
        for p in &pics {
            buf.extend(p.y.to_le_bytes());
            buf.extend(p.u.to_le_bytes());
            buf.extend(p.v.to_le_bytes());
        }
        std::fs::write(out, buf).expect("write output");
    }
}
