import unittest

import speed_groups


class SpeedGroupDomainTests(unittest.TestCase):
    def test_missing_group_defaults_to_standard(self):
        self.assertEqual(speed_groups.normalize_speed_group(None), "standard")
        self.assertEqual(speed_groups.normalize_speed_group(""), "standard")

    def test_known_groups_are_normalized(self):
        self.assertEqual(speed_groups.normalize_speed_group(" STANDARD "), "standard")
        self.assertEqual(speed_groups.normalize_speed_group("FAST"), "fast")

    def test_invalid_group_is_rejected_for_writes(self):
        with self.assertRaises(ValueError):
            speed_groups.require_speed_group("turbo")

    def test_usr_pool_key_uses_litellm_canonical_form(self):
        self.assertEqual(
            speed_groups.canonical_litellm_key("usr_pool_0174_x"),
            "sk-usr_pool_0174_x",
        )

    def test_sk_key_keeps_its_canonical_form(self):
        self.assertEqual(
            speed_groups.canonical_litellm_key("sk-existing"),
            "sk-existing",
        )

    def test_metadata_merge_preserves_existing_fields(self):
        metadata = {
            "email": "u@zilliz.com",
            "model_group": "common",
        }

        self.assertEqual(
            speed_groups.merge_speed_group_metadata(metadata, "fast"),
            {
                "email": "u@zilliz.com",
                "model_group": "common",
                "speed_group": "fast",
            },
        )

    def test_invalid_metadata_is_read_as_standard(self):
        self.assertEqual(
            speed_groups.effective_speed_group({"speed_group": "turbo"}),
            "standard",
        )


if __name__ == "__main__":
    unittest.main()
