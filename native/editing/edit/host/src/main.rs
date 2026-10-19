//! Lumiere's private editing worker. No running desktop application is required.
use std::io::{BufRead, Write};
use lumiere_edit_automation::LumiereEditMcp;
use lumiere_edit_engine::Session;
use serde_json::{Value, json};

#[tokio::main]
async fn main() -> std::io::Result<()> {
    let engine = LumiereEditMcp::headless(Session::default());
    let mut output = std::io::stdout().lock();
    for line in std::io::stdin().lock().lines() {
        let response = match serde_json::from_str::<Value>(&line?) {
            Ok(request) => match request["op"].as_str() {
                Some("catalog") => engine.local_catalog(),
                Some("call") => match engine.local_call(request["call"].as_str().unwrap_or(""), request["args"].clone()).await {
                    Ok(result) => serde_json::to_value(result).unwrap(),
                    Err(error) => json!({"isError":true,"content":[{"type":"text","text":error.to_string()}]}),
                },
                _ => json!({"isError":true,"content":[{"type":"text","text":"Unknown Lumiere worker operation"}]}),
            },
            Err(error) => json!({"isError":true,"content":[{"type":"text","text":error.to_string()}]}),
        };
        writeln!(output, "{response}")?;
        output.flush()?;
    }
    Ok(())
}
