fn main() {
    for lib in ["wpe-webkit-2.0", "wpe-platform-headless-2.0"] {
        pkg_config::Config::new()
            .atleast_version("2.52")
            .probe(lib)
            .unwrap_or_else(|e| panic!("{lib}: {e}"));
    }
}
