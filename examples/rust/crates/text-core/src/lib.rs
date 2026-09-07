/// Join trimmed, nonempty labels with a stable separator.
pub fn summarize<'a>(labels: impl IntoIterator<Item = &'a str>) -> String {
    labels
        .into_iter()
        .map(str::trim)
        .filter(|label| !label.is_empty())
        .collect::<Vec<_>>()
        .join(", ")
}
