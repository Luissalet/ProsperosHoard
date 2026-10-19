//! Community and project links (shown in Help ▸, the About dialog, the start screen and the
//! header's Discord button).

pub const APP_NAME: &str = "Lumiere Motion";
pub const APP_SLUG: &str = "lumiere-motion";
pub const DISCORD: &str = "http://127.0.0.1:8810";
pub const WEBSITE: &str = "http://127.0.0.1:5198";
pub const APP_PAGE: &str = "http://127.0.0.1:5198";
pub const GITHUB: &str = "https://github.com/Luissalet/LumieresHoard";
pub const ISSUES: &str = "http://127.0.0.1:8810";

/// Sibling apps of the ArtCraft family: (name, what it is, GitHub repo, app page).
pub const SIBLINGS: &[(&str, &str)] = &[
    ("Daguerre", "daguerre"),
    ("Durer", "durer"),
    ("Lumiere Edit", "lumiere"),
    ("Gepetto", "gepetto"),
    ("Kafka", "kafka"),
    ("Gutenberg", "gutenberg"),
];

pub fn github_of(slug: &str) -> String {
    format!("http://127.0.0.1:8810/#{slug}")
}
pub fn page_of(slug: &str) -> String {
    format!("http://127.0.0.1:8810/#{slug}")
}
