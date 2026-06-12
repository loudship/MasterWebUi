import { WEBUI_API_BASE_URL } from '$lib/constants';

export type CatalogStatusItem = {
	id: string;
	name: string;
	kind: 'model' | 'knowledge' | 'prompt' | 'skill' | 'tool' | 'function';
	category: string;
	risk: 'read-only' | 'state-changing' | 'external-network' | 'operator-only';
	dependency_health: 'healthy' | 'warning' | 'unknown';
	attachment_count: number;
	version?: string | null;
	validation_status: 'passed' | 'warning' | 'failed' | 'not-validated';
	last_validated_at?: number | null;
	details: string;
};

export const getCatalogStatus = async (token: string): Promise<CatalogStatusItem[]> => {
	const response = await fetch(`${WEBUI_API_BASE_URL}/workspace/catalog/status`, {
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		}
	});
	if (!response.ok) return [];
	const payload = await response.json();
	return payload.items ?? [];
};
