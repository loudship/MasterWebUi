import type { CatalogStatusItem } from '$lib/apis/workspace';

export type CatalogFilters = {
	risk: string;
	dependencyHealth: string;
	attachment: string;
	validationStatus: string;
};

export const catalogStatusMap = (items: CatalogStatusItem[]) =>
	Object.fromEntries(items.map((item) => [item.id, item]));

export const catalogMatches = (
	item: CatalogStatusItem | undefined,
	filters: CatalogFilters
): boolean => {
	if (!item) {
		return !filters.risk && !filters.dependencyHealth && !filters.attachment && !filters.validationStatus;
	}
	if (filters.risk && item.risk !== filters.risk) return false;
	if (filters.dependencyHealth && item.dependency_health !== filters.dependencyHealth) return false;
	if (filters.validationStatus && item.validation_status !== filters.validationStatus) return false;
	if (filters.attachment === 'attached' && item.attachment_count === 0) return false;
	if (filters.attachment === 'unattached' && item.attachment_count > 0) return false;
	return true;
};
