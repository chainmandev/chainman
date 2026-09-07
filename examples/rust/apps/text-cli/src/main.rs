fn main() {
    let labels: Vec<String> = std::env::args().skip(1).collect();
    let summary = text_core::summarize(labels.iter().map(String::as_str));
    println!("{summary}");
}
