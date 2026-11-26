"""The public homepage must be discoverable without exposing private pages."""

from __future__ import annotations

import unittest
from xml.etree import ElementTree

from app import app


class PublicSearchVisibilityTests(unittest.TestCase):
    def test_login_pages_use_local_scripts_and_the_same_security_headers(self) -> None:
        client = app.test_client()
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                with client.get(path) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    policy = response.headers["Content-Security-Policy"]
                    self.assertIn("script-src 'self'", policy)
                    self.assertIn("frame-ancestors 'none'", policy)
                    self.assertIn("form-action 'self'", policy)
                    self.assertNotIn("'unsafe-inline'", policy)
                    html = response.get_data(as_text=True)
                    self.assertIn('src="portal.js?v=2"', html)
                    self.assertNotIn(" onload=", html)
        for path in ("/portal.js", "/portal-motion.js"):
            with client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn("javascript", response.mimetype)
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        with client.get("/bruno-ace-sc-latin.woff2") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data[:4], b"wOF2")

    def test_public_homepage_metadata_and_sitemap(self) -> None:
        client = app.test_client()
        homepage = client.get("/")
        self.assertEqual(homepage.status_code, 200)
        html = homepage.get_data(as_text=True)
        homepage.close()
        self.assertIn("Physics Plus | เรียนพิเศษ ม.ต้น–ม.ปลาย", html)
        self.assertIn('<link rel="canonical" href="https://physicsplus.vercel.app/">', html)
        self.assertIn('name="description"', html)
        self.assertNotIn('name="robots" content="noindex', html)

        robots = client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertIn("Sitemap: https://physicsplus.vercel.app/sitemap.xml", robots.get_data(as_text=True))
        robots.close()
        sitemap = client.get("/sitemap.xml")
        self.assertEqual(sitemap.status_code, 200)
        root = ElementTree.fromstring(sitemap.data)
        urls = [node.text for node in root.iter() if node.tag.endswith("loc")]
        self.assertEqual(urls, ["https://physicsplus.vercel.app/"])
        self.assertNotIn("dashboard.html", sitemap.get_data(as_text=True))
        sitemap.close()


if __name__ == "__main__":
    unittest.main()
