<script lang="ts">
	import Badge from '$lib/components/common/Badge.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import type { CatalogStatusItem } from '$lib/apis/workspace';

	export let item: CatalogStatusItem | null = null;

	const badgeType = (value: string) => {
		if (value === 'healthy' || value === 'passed' || value === 'read-only') return 'success';
		if (value === 'failed' || value === 'state-changing') return 'error';
		if (value === 'warning' || value === 'external-network' || value === 'operator-only') {
			return 'warning';
		}
		return 'muted';
	};
</script>

{#if item}
	<div class="flex flex-wrap gap-1 pt-1" data-catalog-status={item.id}>
		<Tooltip content="Risk classification. Operator-only and state-changing items should be attached manually.">
			<span tabindex="0"><Badge type={badgeType(item.risk)} content={item.risk} /></span>
		</Tooltip>
		<Tooltip content={`Dependency health: ${item.details}`}>
			<span tabindex="0"
				><Badge type={badgeType(item.dependency_health)} content={item.dependency_health} /></span
			>
		</Tooltip>
		<Tooltip content="How many workspace models currently attach this item.">
			<span tabindex="0"><Badge type="info" content={`used by ${item.attachment_count}`} /></span>
		</Tooltip>
		<Tooltip content="Most recent catalog validation status.">
			<span tabindex="0"
				><Badge type={badgeType(item.validation_status)} content={item.validation_status} /></span
			>
		</Tooltip>
		{#if item.version}
			<Tooltip content="Declared catalog version.">
				<span tabindex="0"><Badge type="muted" content={`v${item.version}`} /></span>
			</Tooltip>
		{/if}
	</div>
{/if}
