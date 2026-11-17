//! Opt-in, read-only smoke test against the running Core, through the native UI command path.
//! cargo test --test live_data_flow -- --ignored --nocapture

use omnidesk_lib::commands::exec_command_invoke_body;
use omnidesk_lib::control_client::ControlClient;
use omnidesk_lib::paths::OmnideskPaths;
use serde_json::json;
use tauri::ipc::InvokeBody;

#[test]
#[ignore = "requires a running Core with hydrated plugin-ai-curator data"]
fn native_commands_read_live_workbench_data() {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            let client = ControlClient::new(OmnideskPaths::from_env());
            for (domain, method) in [
                ("media", "fetch_items"),
                ("text", "channels"),
                ("text", "fetch_feed"),
                ("book", "list_books"),
            ] {
                let response = exec_command_invoke_body(&client, "call", &InvokeBody::Json(json!({
                    "args": {
                        "op": "domain.invoke",
                        "params": {"domain": domain, "method": method, "source_id": "plugin-ai-curator", "payload": {}},
                        "timeoutMs": 30000
                    }
                }))).await.unwrap();
                assert_eq!(response["ok"], true, "{domain}.{method}: {}", response["error"]);
                let items = response["data"]["result"].as_array().expect("source returns a list");
                assert!(!items.is_empty(), "{domain}.{method} returned no live data");
                println!("native {domain}.{method}: {} entries", items.len());
            }
        });
}
