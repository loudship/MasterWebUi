import fs from 'node:fs';

const replaceOnce = (path, before, after) => {
	const text = fs.readFileSync(path, 'utf8');
	if (!text.includes(before)) {
		throw new Error(`Patch anchor not found in ${path}: ${before.slice(0, 80)}`);
	}
	fs.writeFileSync(path, text.replace(before, after), 'utf8');
};

const pages = [
	{
		path: 'src/lib/components/workspace/Models.svelte',
		list: '{#each models as model (model.id)}',
		filtered: '{#each models.filter((model) => catalogMatches(catalogStatuses[model.id], catalogFilters())) as model (model.id)}',
		badgeAnchor: '<div class=" flex gap-1 pr-2 -mt-1 items-center">',
		badge: '<CatalogBadges item={catalogStatuses[model.id] ?? null} />\n\t\t\t\t\t\t\t\t\t\t\t<div class=" flex gap-1 pr-2 -mt-1 items-center">'
	},
	{
		path: 'src/lib/components/workspace/Tools.svelte',
		list: '{#each filteredItems as tool}',
		filtered: '{#each filteredItems.filter((tool) => catalogMatches(catalogStatuses[tool.id], catalogFilters())) as tool}',
		badgeAnchor: '<div class="px-0.5">\n\t\t\t\t\t\t\t\t\t\t\t\t<div class="text-xs text-gray-500 shrink-0">',
		badge: '<CatalogBadges item={catalogStatuses[tool.id] ?? null} />\n\t\t\t\t\t\t\t\t\t\t\t<div class="px-0.5">\n\t\t\t\t\t\t\t\t\t\t\t\t<div class="text-xs text-gray-500 shrink-0">'
	},
	{
		path: 'src/lib/components/workspace/Skills.svelte',
		list: '{#each filteredItems as skill}',
		filtered: '{#each filteredItems.filter((skill) => catalogMatches(catalogStatuses[skill.id], catalogFilters())) as skill}',
		badgeAnchor: '<div class="px-0.5">\n\t\t\t\t\t\t\t\t\t\t\t\t<div class="text-xs text-gray-500 shrink-0">',
		badge: '<CatalogBadges item={catalogStatuses[skill.id] ?? null} />\n\t\t\t\t\t\t\t\t\t\t\t<div class="px-0.5">\n\t\t\t\t\t\t\t\t\t\t\t\t<div class="text-xs text-gray-500 shrink-0">'
	},
	{
		path: 'src/lib/components/workspace/Prompts.svelte',
		list: '{#each prompts as prompt (prompt.id)}',
		filtered: '{#each prompts.filter((prompt) => catalogMatches(catalogStatuses[prompt.id], catalogFilters())) as prompt (prompt.id)}',
		badgeAnchor: '<div class="flex gap-1 text-xs">',
		badge: '<CatalogBadges item={catalogStatuses[prompt.id] ?? null} />\n\t\t\t\t\t\t\t<div class="flex gap-1 text-xs">'
	},
	{
		path: 'src/lib/components/workspace/Knowledge.svelte',
		list: '{#each items as item}',
		filtered: '{#each items.filter((item) => catalogMatches(catalogStatuses[item.id], catalogFilters())) as item}',
		badgeAnchor: '<div class=" flex items-center gap-1 justify-between px-1.5">',
		badge: '<CatalogBadges item={catalogStatuses[item.id] ?? null} />\n\t\t\t\t\t\t\t\t\t<div class=" flex items-center gap-1 justify-between px-1.5">'
	}
];

for (const page of pages) {
	const badgeImport = page.path.endsWith('Knowledge.svelte')
		? "import Badge from '../common/Badge.svelte';"
		: "import Badge from '$lib/components/common/Badge.svelte';";
	replaceOnce(
		page.path,
		badgeImport,
		`${badgeImport}\n\timport CatalogBadges from './common/CatalogBadges.svelte';\n\timport CatalogFilters from './common/CatalogFilters.svelte';\n\timport { getCatalogStatus } from '$lib/apis/workspace';\n\timport { catalogMatches, catalogStatusMap } from './common/catalog';`
	);
	const variableAnchor = page.path.endsWith('Models.svelte') ? 'let shiftKey = false;' : 'let loaded = false;';
	replaceOnce(
		page.path,
		variableAnchor,
		`${variableAnchor}\n\tlet catalogStatuses = {};\n\tlet riskFilter = '';\n\tlet dependencyHealthFilter = '';\n\tlet attachmentFilter = '';\n\tlet validationStatusFilter = '';\n\tconst catalogFilters = () => ({ risk: riskFilter, dependencyHealth: dependencyHealthFilter, attachment: attachmentFilter, validationStatus: validationStatusFilter });`
	);
	replaceOnce(
		page.path,
		'onMount(async () => {',
		"onMount(async () => {\n\t\tcatalogStatuses = catalogStatusMap(await getCatalogStatus(localStorage.token).catch(() => []));"
	);
	replaceOnce(page.path, page.list, page.filtered);
	replaceOnce(page.path, page.badgeAnchor, page.badge);

	const filterAnchor = page.path.endsWith('Models.svelte')
		? '\n\t\t{#if models !== null}'
		: page.path.endsWith('Tools.svelte')
			? '\n\t\t{#if (filteredItems ?? []).length !== 0}'
			: page.path.endsWith('Skills.svelte')
				? '\n\t\t{#if filteredItems === null || loading}'
				: page.path.endsWith('Prompts.svelte')
					? '\n\t\t{#if prompts === null || loading}'
					: '\n\t\t{#if items !== null && total !== null}';
	replaceOnce(
		page.path,
		filterAnchor,
		`\n\t\t<CatalogFilters bind:risk={riskFilter} bind:dependencyHealth={dependencyHealthFilter} bind:attachment={attachmentFilter} bind:validationStatus={validationStatusFilter} />${filterAnchor}`
	);
}

const layout = 'src/routes/(app)/workspace/+layout.svelte';
replaceOnce(
	layout,
	"} else if ($page.url.pathname.includes('/skills') && !$user?.permissions?.workspace?.skills) {",
	"} else if ($page.url.pathname.includes('/functions')) {\n\t\t\t\tgoto('/');\n\t\t\t} else if ($page.url.pathname.includes('/skills') && !$user?.permissions?.workspace?.skills) {"
);
replaceOnce(
	layout,
	"\t\t\t\t\t\t{#if $user?.role === 'admin' || $user?.permissions?.workspace?.tools}",
	`\t\t\t\t\t\t{#if $user?.role === 'admin'}
\t\t\t\t\t\t\t<a
\t\t\t\t\t\t\t\tdraggable="false"
\t\t\t\t\t\t\t\taria-current={$page.url.pathname.includes('/workspace/functions') ? 'page' : null}
\t\t\t\t\t\t\t\tclass="min-w-fit p-1.5 {$page.url.pathname.includes('/workspace/functions')
\t\t\t\t\t\t\t\t\t? ''
\t\t\t\t\t\t\t\t\t: 'text-gray-300 dark:text-gray-600 hover:text-gray-700 dark:hover:text-white'} transition select-none"
\t\t\t\t\t\t\t\ttitle="Runtime filters, actions, and pipeline functions."
\t\t\t\t\t\t\t\thref="/workspace/functions">{$i18n.t('Functions')}</a
\t\t\t\t\t\t\t>
\t\t\t\t\t\t{/if}

\t\t\t\t\t\t{#if $user?.role === 'admin' || $user?.permissions?.workspace?.tools}`
);
for (const [href, title] of [
	['/workspace/models', 'Reusable model presets and their attached capabilities.'],
	['/workspace/knowledge', 'Curated documents that models can retrieve as context.'],
	['/workspace/prompts', 'Reusable slash-command prompt templates.'],
	['/workspace/skills', 'Reusable operating instructions loaded by models on demand.'],
	['/workspace/tools', 'Executable integrations. Review risk badges before attaching them.']
]) {
	replaceOnce(layout, `href="${href}"`, `title="${title}"\n\t\t\t\t\t\t\t\thref="${href}"`);
}
replaceOnce(
	layout,
	'\t\t<div\n\t\t\tclass="  pb-1 px-3 md:px-[18px] flex-1 max-h-full overflow-y-auto"',
	'\t\t<div class="px-4 pt-1 text-xs text-gray-500">Workspace catalog: reusable presets, context, prompts, skills, and executable integrations. Hover or focus badges for safety and dependency details.</div>\n\t\t<div\n\t\t\tclass="  pb-1 px-3 md:px-[18px] flex-1 max-h-full overflow-y-auto"'
);

const functionsPage = 'src/lib/components/admin/Functions.svelte';
replaceOnce(
	functionsPage,
	"import Spinner from '../common/Spinner.svelte';",
	"import Spinner from '../common/Spinner.svelte';\n\timport CatalogBadges from '../workspace/common/CatalogBadges.svelte';\n\timport CatalogFilters from '../workspace/common/CatalogFilters.svelte';\n\timport { getCatalogStatus } from '$lib/apis/workspace';\n\timport { catalogMatches, catalogStatusMap } from '../workspace/common/catalog';"
);
replaceOnce(
	functionsPage,
	"let loaded = false;",
	"let loaded = false;\n\tlet catalogStatuses = {};\n\tlet riskFilter = '';\n\tlet dependencyHealthFilter = '';\n\tlet attachmentFilter = '';\n\tlet validationStatusFilter = '';\n\tconst catalogFilters = () => ({ risk: riskFilter, dependencyHealth: dependencyHealthFilter, attachment: attachmentFilter, validationStatus: validationStatusFilter });"
);
replaceOnce(
	functionsPage,
	"onMount(async () => {",
	"onMount(async () => {\n\t\tcatalogStatuses = catalogStatusMap(await getCatalogStatus(localStorage.token).catch(() => []));"
);
replaceOnce(
	functionsPage,
	"{#if (filteredItems ?? []).length !== 0}",
	"<CatalogFilters bind:risk={riskFilter} bind:dependencyHealth={dependencyHealthFilter} bind:attachment={attachmentFilter} bind:validationStatus={validationStatusFilter} />\n\t\t\t{#if (filteredItems ?? []).length !== 0}"
);
replaceOnce(
	functionsPage,
	"{#each filteredItems as func (func.id)}",
	"{#each filteredItems.filter((func) => catalogMatches(catalogStatuses[func.id], catalogFilters())) as func (func.id)}"
);
replaceOnce(
	functionsPage,
	'<div class=" flex items-center gap-1.5">',
	'<CatalogBadges item={catalogStatuses[func.id] ?? null} />\n\t\t\t\t\t\t\t\t\t\t\t<div class=" flex items-center gap-1.5">'
);
