use text_core::summarize;

#[test]
fn omits_blank_labels() {
    assert_eq!(summarize([" first ", "", " second"]), "first, second");
}
