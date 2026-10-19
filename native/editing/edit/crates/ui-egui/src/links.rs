//! Community and project links (Help menu, About dialog, header Discord button, Home screen).
//!
//! Product links belong to Lumiere and the Hoard Hub.

/// The app's short name, used in the website and repository URLs.
pub const APP: &str = "lumiere-edit";

/// The ArtCraft community Discord.
pub const DISCORD: &str = "http://127.0.0.1:8810";
/// The ArtCraft website.
pub const WEBSITE: &str = "http://127.0.0.1:5198";
/// Lumiere Edit's page on the ArtCraft website.
pub const APP_PAGE: &str = WEBSITE;
/// Lumiere Edit's source repository.
pub const GITHUB: &str = "https://github.com/Luissalet/LumieresHoard";
/// New issue on Lumiere Edit's repository.
pub const ISSUES: &str = "http://127.0.0.1:8810";

/// (command id, label, url) for every link, in menu order.
pub const ALL: [(&str, &str, &str); 5] = [
    ("help.discord", "Hoard Hub", DISCORD),
    ("help.website", "Lumiere", WEBSITE),
    ("help.appPage", "Lumiere editing", APP_PAGE),
    ("help.github", "Lumiere Edit on GitHub", GITHUB),
    ("help.reportIssue", "Report an Issue…", ISSUES),
];

/// The URL a `help.*` link command opens.
pub fn url_for(command: &str) -> Option<&'static str> {
    ALL.iter().find(|(id, _, _)| *id == command).map(|(_, _, u)| *u)
}

/// Open `url` in the system browser (a new tab on the web).
pub fn open(ctx: &egui::Context, url: &str) {
    ctx.open_url(egui::OpenUrl::new_tab(url));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn product_links_belong_to_lumiere_and_the_hub() {
        assert_eq!(APP_PAGE, WEBSITE);
        assert_eq!(GITHUB, "https://github.com/Luissalet/LumieresHoard");
        assert!(ALL.iter().all(|(id, _, url)| id.starts_with("help.") && (url.starts_with("https://github.com/Luissalet/") || url.starts_with("http://127.0.0.1:"))));
        assert_eq!(url_for("help.discord"), Some(DISCORD));
        assert_eq!(url_for("help.nope"), None);
    }
}
