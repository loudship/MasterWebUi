<script lang="ts">
	export let risk = '';
	export let dependencyHealth = '';
	export let attachment = '';
	export let validationStatus = '';
</script>

<div class="flex flex-wrap gap-1.5 px-3 pb-2" aria-label="Catalog posture filters">
	<select class="text-xs rounded-lg bg-transparent border px-2 py-1" bind:value={risk} title="Filter by risk classification">
		<option value="">All risks</option>
		<option value="read-only">Read-only</option>
		<option value="state-changing">State-changing</option>
		<option value="external-network">External network</option>
		<option value="operator-only">Operator-only</option>
	</select>
	<select class="text-xs rounded-lg bg-transparent border px-2 py-1" bind:value={dependencyHealth} title="Filter by dependency health">
		<option value="">All dependencies</option>
		<option value="healthy">Healthy</option>
		<option value="warning">Warning</option>
		<option value="unknown">Unknown</option>
	</select>
	<select class="text-xs rounded-lg bg-transparent border px-2 py-1" bind:value={attachment} title="Filter by model attachment state">
		<option value="">All attachments</option>
		<option value="attached">Attached to a model</option>
		<option value="unattached">Not attached</option>
	</select>
	<select class="text-xs rounded-lg bg-transparent border px-2 py-1" bind:value={validationStatus} title="Filter by latest validation result">
		<option value="">All validation states</option>
		<option value="passed">Passed</option>
		<option value="warning">Warning</option>
		<option value="failed">Failed</option>
		<option value="not-validated">Not validated</option>
	</select>
</div>
