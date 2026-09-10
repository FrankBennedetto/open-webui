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

	$: src =
		file?.url?.startsWith('data') || file?.url?.startsWith('http')
			? file.url
			: `${WEBUI_API_BASE_URL}/files/${file.url}${file?.content_type ? '/content' : ''}`;
</script>

<video
	src={src}
	controls
	playsinline
	{preload}
	class={className}
	aria-label={file?.name || 'Video'}
></video>
