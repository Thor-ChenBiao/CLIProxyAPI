from litellm.integrations.custom_logger import CustomLogger


class SpeedGroupCallback(CustomLogger):
    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data,
        call_type,
    ):
        request_data = dict(data or {})
        if request_data.get("service_tier") not in (None, ""):
            return request_data

        metadata = getattr(user_api_key_dict, "metadata", None) or {}
        speed_group = str(metadata.get("speed_group") or "").strip().lower()
        if speed_group == "fast":
            request_data["service_tier"] = "priority"
        return request_data


speed_group_callback = SpeedGroupCallback()
