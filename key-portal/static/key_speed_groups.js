(function exposeKeySpeedGroups(global) {
    function normalize(value) {
        return value === 'fast' ? 'fast' : 'standard';
    }

    async function update(key, speedGroup) {
        const response = await fetch('/api/keys/speed-group', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                key,
                speed_group: normalize(speedGroup)
            })
        });
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error || 'Request failed');
        }
        return payload;
    }

    global.KeySpeedGroups = Object.freeze({ normalize, update });
})(window);
