<script lang="ts">
	import { WEBUI_API_BASE_URL } from '$lib/constants';

	export let file: {
		url?: string;
		name?: string;
		content_type?: string;
	} = {};
	/** Tailwind / utility classes for the video element */
	export let className = 'max-h-96 rounded-lg';
	/** Prefer metadata for compose thumbs; auto for in-bubble playback */
	export let preload: 'none' | 'metadata' | 'auto' = 'metadata';

	/**
	 * Resolve a playable src once:
	 * - data:/http(s): absolute URLs → as-is
	 * - paths starting with `/` or already containing `/files/` → as-is (avoid double-wrap)
	 * - bare file ids → `${WEBUI_API_BASE_URL}/files/{id}/content` when content_type is set
	 */
	function resolveSrc(url?: string, content_type?: string): string {
		if (!url) return '';
		if (
			url.startsWith('data:') ||
			url.startsWith('http://') ||
			url.startsWith('https://') ||
			url.startsWith('/') ||
			url.includes('/files/')
		) {
			return url;
		}
		return `${WEBUI_API_BASE_URL}/files/${url}${content_type ? '/content' : ''}`;
	}

	$: src = resolveSrc(file?.url, file?.content_type);
</script>

<video
	src={src}
	controls
	playsinline
	{preload}
	class={className}
	aria-label={file?.name || 'Video'}
></video>
