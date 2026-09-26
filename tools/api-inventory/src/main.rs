fn main() -> Result<(), Box<dyn std::error::Error>> {
    let input = std::env::args_os()
        .nth(1)
        .ok_or("rustdoc JSON path required")?;
    let document: serde_json::Value = serde_json::from_slice(&std::fs::read(&input)?)?;
    let root = document["root"].to_string();
    let own_crate = &document["index"][&root]["crate_id"];
    let api = public_api::Builder::from_rustdoc_json(input).build()?;
    // rustdoc keeps external traits such as Send as path references. They are
    // intentionally not expanded into this crate's declarations. An unresolved
    // own-crate item (or an unknown reference) must never silently disappear.
    for id in api.missing_item_ids() {
        let path = &document["paths"][id.to_string()];
        if path.is_null() || &path["crate_id"] == own_crate {
            return Err("rustdoc omitted a local or unresolved public item".into());
        }
    }
    print!("{api}");
    Ok(())
}
