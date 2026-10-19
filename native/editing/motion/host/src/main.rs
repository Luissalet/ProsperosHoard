//! Lumiere's private compositor worker. The engine is linked into this process.
use std::io::{BufRead, Write};
use lumiere_motion_automation::{Backend, tools, server::call_result};
use serde_json::{Value, json};

fn main() -> std::io::Result<()> {
    let mut engine = Backend::headless(lumiere_motion_host::session());
    let mut output = std::io::stdout().lock();
    for line in std::io::stdin().lock().lines() {
        let response = match serde_json::from_str::<Value>(&line?) {
            Ok(request) => match request["op"].as_str() {
                Some("catalog") => json!({"tools": tools::available(false).map(|tool| tool.descriptor()).collect::<Vec<_>>(),
                                         "engine": "motion", "protocol": "lumiere-worker/1"}),
                Some("call") => call_result(tools::run(&mut engine, request["call"].as_str().unwrap_or(""), &request["args"])),
                _ => json!({"isError": true, "content": [{"type":"text","text":"Unknown Lumiere worker operation"}]}),
            },
            Err(error) => json!({"isError": true, "content": [{"type":"text","text":error.to_string()}]}),
        };
        writeln!(output, "{response}")?;
        output.flush()?;
    }
    Ok(())
}
