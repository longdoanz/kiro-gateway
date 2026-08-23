"use client";

import { useEffect, useState } from "react";
import { Save, RefreshCw, Trash2, Plus, Key, ArrowRight, Pencil, List, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useConfig, useUpdateConfig } from "@/hooks/use-config";
import { useSystemKeys, useCreateSystemKey, useUpdateSystemKey, useDeleteSystemKey, useSystemKeyPool, useStickyBindings } from "@/hooks/use-system-keys";
import { useModels } from "@/hooks/use-models";
import { maskKey } from "@/lib/utils";
import type { ModelOverrideRule } from "@/lib/types";

function AddSystemKeyDialog() {
  const [rawKey, setRawKey] = useState("");
  const [useProxy, setUseProxy] = useState(false);
  const [open, setOpen] = useState(false);
  const createKey = useCreateSystemKey();

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    await createKey.mutateAsync({
      raw_key: rawKey,
      use_proxy: useProxy,
    });
    setRawKey("");
    setUseProxy(false);
    setOpen(false);
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button size="sm" className="gap-2" />}>
        <Plus className="w-4 h-4" /> Add Kiro Key
      </DialogTrigger>
      <DialogContent className="glass-panel-elevated max-w-md w-full">
        <DialogHeader>
          <DialogTitle>Register System API Key</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4 mt-4">
          <div className="space-y-2">
            <Label htmlFor="raw_key">Kiro API Key</Label>
            <Input
              id="raw_key"
              value={rawKey}
              onChange={(e) => setRawKey(e.target.value)}
              placeholder="sk-proj-..."
              className="font-mono text-sm"
              required
              minLength={10}
            />
          </div>
          <div className="flex items-center justify-between">
            <div className="space-y-0.5">
              <Label>Use Proxy</Label>
              <p className="text-xs text-on-surface-variant">Enable if this key requires a proxy</p>
            </div>
            <Switch checked={useProxy} onCheckedChange={setUseProxy} />
          </div>
          <Button type="submit" disabled={createKey.isPending} className="w-full">
            {createKey.isPending ? "Registering..." : "Register Key"}
          </Button>
          {createKey.isError && (
            <p className="text-sm text-error">
              {(createKey.error as any)?.response?.data?.detail || "Failed to register key"}
            </p>
          )}
        </form>
      </DialogContent>
    </Dialog>
  );
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "K";
  return n.toString();
}

function SystemKeysSection() {
  const { data: keys, isLoading } = useSystemKeys();
  const updateKey = useUpdateSystemKey();
  const deleteKey = useDeleteSystemKey();

  if (isLoading) {
    return <Skeleton className="h-48 rounded-3xl" />;
  }

  return (
    <div className="glass-panel rounded-3xl p-8 md:p-10 group relative overflow-hidden">
      <div className="absolute inset-0 bg-gradient-to-br from-white/40 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none" />
      <div className="flex items-center justify-between mb-6">
        <div>
          <h3 className="text-lg font-semibold text-on-surface flex items-center gap-2">
            <Key className="w-5 h-5 text-sky-600" />
            System Kiro Keys
          </h3>
          <p className="text-sm text-on-surface-variant mt-1">
            Manage system-level backup keys used for the fallback mechanism.
          </p>
        </div>
        <AddSystemKeyDialog />
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left border-collapse">
          <thead>
            <tr className="border-b border-outline-variant/30">
              <th className="py-3 px-4 font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider">Key</th>
              <th className="py-3 px-4 font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider text-right">Credits (month)</th>
              <th className="py-3 px-4 font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider text-right">Tokens (30d)</th>
              <th className="py-3 px-4 font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider">Last Used</th>
              <th className="py-3 px-4 font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider text-center">Proxy</th>
              <th className="py-3 px-4 font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider text-center">Status</th>
              <th className="py-3 px-4 text-right"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-outline-variant/20">
            {keys?.map((key) => (
              <tr key={key.id} className="hover:bg-surface-container-lowest/50 transition-colors">
                <td className="py-3 px-4 font-mono text-xs text-on-surface">
                  {maskKey(key.key_prefix, key.key_suffix)}
                </td>
                <td className="py-3 px-4 text-right text-sm tabular-nums text-on-surface">
                  {key.current_usage.toLocaleString()}
                  {key.usage_limit > 0 && (
                    <span className="text-on-surface-variant"> / {key.usage_limit.toLocaleString()}</span>
                  )}
                </td>
                <td className="py-3 px-4 text-right text-xs tabular-nums text-on-surface-variant whitespace-nowrap">
                  <span className="text-sky-700">{fmtTokens(key.input_tokens ?? 0)}</span>
                  {" in / "}
                  <span className="text-emerald-700">{fmtTokens(key.output_tokens ?? 0)}</span>
                  {" out"}
                </td>
                <td className="py-3 px-4 text-xs text-on-surface-variant whitespace-nowrap">
                  {key.last_used_at
                    ? new Date(key.last_used_at).toLocaleString()
                    : <span className="italic">Never</span>}
                </td>
                <td className="py-3 px-4 text-center">
                  <Switch
                    checked={key.use_proxy}
                    onCheckedChange={(checked) =>
                      updateKey.mutate({ keyId: key.id, data: { use_proxy: checked } })
                    }
                    disabled={updateKey.isPending}
                    className="scale-75"
                  />
                </td>
                <td className="py-3 px-4 text-center">
                  <Switch
                    checked={key.is_active}
                    onCheckedChange={(checked) =>
                      updateKey.mutate({ keyId: key.id, data: { is_active: checked } })
                    }
                    disabled={updateKey.isPending}
                    className="scale-75"
                  />
                </td>
                <td className="py-3 px-4 text-right">
                  <button
                    onClick={() => {
                      if (confirm("Are you sure you want to delete this system key?")) {
                        deleteKey.mutate(key.id);
                      }
                    }}
                    disabled={deleteKey.isPending}
                    className="text-on-surface-variant hover:text-error transition-colors p-1"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </td>
              </tr>
            ))}
            {keys?.length === 0 && (
              <tr>
                <td colSpan={7} className="py-8 text-center text-sm text-on-surface-variant italic">
                  No system keys registered yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SystemKeyLiveStatus() {
  const { data: pool, isLoading: poolLoading } = useSystemKeyPool();
  const { data: bindings, isLoading: bindingsLoading } = useStickyBindings();

  if (poolLoading || bindingsLoading) {
    return <Skeleton className="h-32 rounded-3xl" />;
  }

  return (
    <div className="glass-panel rounded-3xl p-8 md:p-10 group relative overflow-hidden">
      <div className="absolute inset-0 bg-gradient-to-br from-white/40 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none" />
      <h3 className="text-lg font-semibold text-on-surface flex items-center gap-2 mb-6">
        <Key className="w-5 h-5 text-emerald-600" />
        Live Key Pool Status
      </h3>

      <div className="space-y-6">
        {/* Pool */}
        <div>
          <p className="text-sm font-medium text-on-surface-variant mb-3">System Keys in Pool</p>
          {!pool?.length ? (
            <p className="text-sm text-on-surface-variant italic">No system keys in cache.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="border-b border-outline-variant/30">
                    <th className="py-2 px-3 text-xs text-on-surface-variant uppercase tracking-wider">Key ID</th>
                    <th className="py-2 px-3 text-xs text-on-surface-variant uppercase tracking-wider text-right">Usage</th>
                    <th className="py-2 px-3 text-xs text-on-surface-variant uppercase tracking-wider text-right">Remaining</th>
                    <th className="py-2 px-3 text-xs text-on-surface-variant uppercase tracking-wider text-center">Proxy</th>
                    <th className="py-2 px-3 text-xs text-on-surface-variant uppercase tracking-wider text-center">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-outline-variant/20">
                  {pool.map((entry) => (
                    <tr key={entry.key_id} className="hover:bg-surface-container-lowest/50 transition-colors">
                      <td className="py-2 px-3 font-mono text-xs text-on-surface">#{entry.key_id}</td>
                      <td className="py-2 px-3 text-right text-xs tabular-nums text-on-surface">
                        {entry.current_usage.toLocaleString()} / {entry.usage_limit > 0 ? entry.usage_limit.toLocaleString() : "∞"}
                      </td>
                      <td className="py-2 px-3 text-right text-xs tabular-nums">
                        <span className={entry.remaining !== null && entry.remaining < 100 ? "text-amber-600 font-medium" : "text-emerald-600"}>
                          {entry.remaining !== null ? entry.remaining.toLocaleString() : "∞"}
                        </span>
                      </td>
                      <td className="py-2 px-3 text-center text-xs">
                        {entry.use_proxy ? <span className="text-sky-600">yes</span> : <span className="text-on-surface-variant">no</span>}
                      </td>
                      <td className="py-2 px-3 text-center text-xs">
                        {!entry.is_active ? (
                          <span className="text-error">inactive</span>
                        ) : entry.quota_exhausted_for_seconds !== null ? (
                          <span className="text-amber-600">cooldown {entry.quota_exhausted_for_seconds}s</span>
                        ) : (
                          <span className="text-emerald-600">active</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Bindings */}
        <div>
          <p className="text-sm font-medium text-on-surface-variant mb-3">Active Sticky Bindings</p>
          {!bindings?.length ? (
            <p className="text-sm text-on-surface-variant italic">No active bindings.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="border-b border-outline-variant/30">
                    <th className="py-2 px-3 text-xs text-on-surface-variant uppercase tracking-wider">Gateway Key</th>
                    <th className="py-2 px-3 text-xs text-on-surface-variant uppercase tracking-wider">Using System Key</th>
                    <th className="py-2 px-3 text-xs text-on-surface-variant uppercase tracking-wider text-right">Expires in</th>
                    <th className="py-2 px-3 text-xs text-on-surface-variant uppercase tracking-wider text-right">Usage</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-outline-variant/20">
                  {bindings.map((b) => (
                    <tr key={b.gateway_key_id} className="hover:bg-surface-container-lowest/50 transition-colors">
                      <td className="py-2 px-3 font-mono text-xs text-on-surface">#{b.gateway_key_id}</td>
                      <td className="py-2 px-3 font-mono text-xs text-sky-700">#{b.system_key_id}</td>
                      <td className="py-2 px-3 text-right text-xs tabular-nums text-on-surface-variant">
                        {b.expires_in_seconds}s
                      </td>
                      <td className="py-2 px-3 text-right text-xs tabular-nums text-on-surface">
                        {b.current_usage !== null ? `${b.current_usage.toLocaleString()} / ${b.usage_limit?.toLocaleString() ?? "∞"}` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

interface ModelSelectProps {
  value: string;
  onChange: (v: string) => void;
  modelIds: string[];
  placeholder?: string;
  /** When true, the user can switch from the dropdown to a free-text input to type any value. */
  allowFreeText?: boolean;
}

function ModelSelect({ value, onChange, modelIds, placeholder = "Select model...", allowFreeText = false }: ModelSelectProps) {
  const [freeText, setFreeText] = useState(allowFreeText ? !modelIds.includes(value) && value !== "" : false);

  if (allowFreeText && freeText) {
    return (
      <div className="flex items-center gap-1">
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="font-mono text-xs h-8 min-w-[200px]"
        />
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          onClick={() => setFreeText(false)}
          title="Switch to dropdown"
        >
          <List className="w-3 h-3" />
        </Button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1">
      <Select value={value} onValueChange={(v) => onChange(v ?? "auto")}>
        <SelectTrigger className="font-mono text-xs h-8 min-w-[200px]">
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="auto">auto</SelectItem>
          {modelIds.map((id) => (
            <SelectItem key={id} value={id}>{id}</SelectItem>
          ))}
        </SelectContent>
      </Select>
      {allowFreeText && (
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          onClick={() => setFreeText(true)}
          title="Type a custom value"
        >
          <Pencil className="w-3 h-3" />
        </Button>
      )}
    </div>
  );
}

interface ModelOverrideSectionProps {
  enabled: boolean;
  onEnabledChange: (v: boolean) => void;
  rules: ModelOverrideRule[];
  onRulesChange: (rules: ModelOverrideRule[]) => void;
  defaultModel: string;
  onDefaultModelChange: (v: string) => void;
  modelIds: string[];
  title?: string;
  description?: string;
}

function ModelOverrideSection({
  enabled,
  onEnabledChange,
  rules,
  onRulesChange,
  defaultModel,
  onDefaultModelChange,
  modelIds,
  title = "Global Model Enforcement",
  description = "Override models on all API requests. Rules are matched by substring (first match wins). The default applies when no rule matches.",
}: ModelOverrideSectionProps) {
  function addRule() {
    onRulesChange([...rules, { from: modelIds[0] ?? "auto", to: "auto" }]);
  }

  function updateRule(index: number, field: "from" | "to", value: string) {
    const updated = rules.map((r, i) => (i === index ? { ...r, [field]: value } : r));
    onRulesChange(updated);
  }

  // Normalize a rule's `to` into an array of fallback targets (ordered).
  function targetsOf(rule: ModelOverrideRule): string[] {
    return Array.isArray(rule.to) ? rule.to : [rule.to];
  }

  function setTargets(index: number, targets: string[]) {
    const updated = rules.map((r, i) => (i === index ? { ...r, to: targets.length === 1 ? targets[0] : targets } : r));
    onRulesChange(updated);
  }

  function addTarget(index: number) {
    const targets = targetsOf(rules[index]);
    setTargets(index, [...targets, "auto"]);
  }

  function updateTarget(index: number, targetIndex: number, value: string) {
    const targets = targetsOf(rules[index]);
    const next = targets.map((t, i) => (i === targetIndex ? value : t));
    setTargets(index, next);
  }

  function removeTarget(index: number, targetIndex: number) {
    const targets = targetsOf(rules[index]);
    if (targets.length <= 1) return;
    setTargets(index, targets.filter((_, i) => i !== targetIndex));
  }

  function removeRule(index: number) {
    onRulesChange(rules.filter((_, i) => i !== index));
  }

  return (
    <div className="glass-panel rounded-3xl p-8 md:p-10 group relative overflow-hidden">
      <div className="absolute inset-0 bg-gradient-to-br from-white/40 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none" />
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-lg font-semibold text-on-surface">{title}</h3>
          <p className="text-sm text-on-surface-variant mt-1 max-w-lg">{description}</p>
        </div>
        <Switch checked={enabled} onCheckedChange={onEnabledChange} />
      </div>

      {enabled && (
        <div className="mt-6 space-y-5">
          {/* Default model */}
          <div className="space-y-2">
            <Label>Default Model</Label>
            <p className="text-xs text-on-surface-variant">Applied when no rule matches the requested model.</p>
            <ModelSelect
              value={defaultModel}
              onChange={onDefaultModelChange}
              modelIds={modelIds}
              placeholder="Select default model..."
            />
          </div>

          {/* Rules table */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>Override Rules</Label>
              <Button size="sm" variant="outline" onClick={addRule} className="gap-1 h-7 text-xs">
                <Plus className="w-3 h-3" /> Add Rule
              </Button>
            </div>
            <p className="text-xs text-on-surface-variant">
              First matching rule wins. &quot;From&quot; is matched as a substring of the normalized model name.
              Each rule&apos;s targets are tried in order on failure.
            </p>

            {rules.length === 0 ? (
              <p className="text-sm text-on-surface-variant italic py-3">
                No rules — only the default model applies.
              </p>
            ) : (
              <div className="space-y-2 mt-2">
                {rules.map((rule, i) => (
                  <div key={i} className="flex flex-wrap items-center gap-2">
                    <span className="text-xs text-on-surface-variant w-5 text-right shrink-0">{i + 1}.</span>
                    <ModelSelect
                      value={rule.from}
                      onChange={(v) => updateRule(i, "from", v)}
                      modelIds={modelIds}
                      placeholder="Match model..."
                      allowFreeText
                    />
                    <ArrowRight className="w-4 h-4 text-on-surface-variant shrink-0" />
                    <div className="flex items-center gap-1">
                      {targetsOf(rule).map((target, ti) => (
                        <div key={ti} className="flex items-center gap-1">
                          {ti > 0 && <ArrowRight className="w-3 h-3 text-on-surface-variant/60 shrink-0" />}
                          <ModelSelect
                            value={target}
                            onChange={(v) => updateTarget(i, ti, v)}
                            modelIds={modelIds}
                            placeholder="Replace with..."
                            allowFreeText
                          />
                          {targetsOf(rule).length > 1 && (
                            <button
                              type="button"
                              onClick={() => removeTarget(i, ti)}
                              className="text-on-surface-variant hover:text-error transition-colors p-1 shrink-0"
                              title="Remove fallback target"
                            >
                              <X className="w-3 h-3" />
                            </button>
                          )}
                        </div>
                      ))}
                      <button
                        type="button"
                        onClick={() => addTarget(i)}
                        className="text-on-surface-variant hover:text-primary transition-colors p-1 shrink-0"
                        title="Add fallback target"
                      >
                        <Plus className="w-3 h-3" />
                      </button>
                    </div>
                    <button
                      onClick={() => removeRule(i)}
                      className="text-on-surface-variant hover:text-error transition-colors p-1 shrink-0"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function SettingsPage() {
  const { data: config, isLoading, refetch } = useConfig();
  const { data: modelsData } = useModels();
  const updateConfig = useUpdateConfig();

  const [enableModelOverride, setEnableModelOverride] = useState(false);
  const [overrideRules, setOverrideRules] = useState<ModelOverrideRule[]>([]);
  const [defaultModel, setDefaultModel] = useState("auto");
  const [enableUsageSharing, setEnableUsageSharing] = useState(false);
  const [enableNineRouterModelOverride, setEnableNineRouterModelOverride] = useState(false);
  const [nineRouterOverrideRules, setNineRouterOverrideRules] = useState<ModelOverrideRule[]>([]);
  const [nineRouterDefaultModel, setNineRouterDefaultModel] = useState("auto");
  const [enableNineRouterDirect, setEnableNineRouterDirect] = useState(false);
  const [dirty, setDirty] = useState(false);

  const modelIds = (modelsData?.models ?? []).map((m) => m.id);

  useEffect(() => {
    if (config) {
      setEnableModelOverride(config.enable_model_override);
      setOverrideRules(config.model_override_rules ?? []);
      setDefaultModel(config.model_override_default ?? "auto");
      setEnableUsageSharing(config.enable_usage_sharing);
      setEnableNineRouterModelOverride(config.enable_nine_router_model_override ?? false);
      setNineRouterOverrideRules(config.nine_router_model_override_rules ?? []);
      setNineRouterDefaultModel(config.nine_router_model_override_default ?? "auto");
      setEnableNineRouterDirect(config.enable_nine_router_direct ?? false);
      setDirty(false);
    }
  }, [config]);

  function handleChange<T>(setter: (v: T) => void) {
    return (v: T) => {
      setter(v);
      setDirty(true);
    };
  }

  async function handleSave() {
    await updateConfig.mutateAsync({
      enable_model_override: enableModelOverride,
      model_override_rules: overrideRules,
      model_override_default: defaultModel,
      enable_usage_sharing: enableUsageSharing,
      enable_nine_router_model_override: enableNineRouterModelOverride,
      nine_router_model_override_rules: nineRouterOverrideRules,
      nine_router_model_override_default: nineRouterDefaultModel,
      enable_nine_router_direct: enableNineRouterDirect,
    });
    setDirty(false);
  }

  if (isLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-10 w-64" />
        {[1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-32 rounded-2xl" />
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <div className="flex justify-end">
        <div className="flex gap-3">
          <Button variant="outline" size="sm" onClick={() => refetch()} className="gap-2">
            <RefreshCw className="w-4 h-4" /> Reload
          </Button>
          <Button size="sm" onClick={handleSave} disabled={!dirty || updateConfig.isPending} className="gap-2">
            <Save className="w-4 h-4" />
            {updateConfig.isPending ? "Saving..." : "Apply Changes"}
          </Button>
        </div>
      </div>

      <ModelOverrideSection
        enabled={enableModelOverride}
        onEnabledChange={handleChange(setEnableModelOverride)}
        rules={overrideRules}
        onRulesChange={handleChange(setOverrideRules)}
        defaultModel={defaultModel}
        onDefaultModelChange={handleChange(setDefaultModel)}
        modelIds={modelIds}
      />

      {/* Usage Sharing / Fallback Section */}
      <div className="glass-panel rounded-3xl p-8 md:p-10 group relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-br from-white/40 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none" />
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-lg font-semibold text-on-surface">Usage Sharing (Fallback)</h3>
            <p className="text-sm text-on-surface-variant mt-1 max-w-lg">
              When enabled, the gateway will automatically use round-robin fallback to borrow a backup
              key when the primary key is below 1% of its usage limit.
            </p>
          </div>
          <Switch
            checked={enableUsageSharing}
            onCheckedChange={handleChange(setEnableUsageSharing)}
          />
        </div>
      </div>

      {/* 9Router Direct Mode Section */}
      <div className="glass-panel rounded-3xl p-8 md:p-10 group relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-br from-white/40 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none" />
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-lg font-semibold text-on-surface">Use 9router directly</h3>
            <p className="text-sm text-on-surface-variant mt-1 max-w-lg">
              When enabled, every API request is forwarded straight to 9router, bypassing the Kiro
              account/key pool entirely. The 9Router Model Override rules below still apply on the
              forwarded requests.
            </p>
          </div>
          <Switch
            checked={enableNineRouterDirect}
            onCheckedChange={handleChange(setEnableNineRouterDirect)}
          />
        </div>
      </div>

      {/* 9Router Model Override Section */}
      <ModelOverrideSection
        enabled={enableNineRouterModelOverride}
        onEnabledChange={handleChange(setEnableNineRouterModelOverride)}
        rules={nineRouterOverrideRules}
        onRulesChange={handleChange(setNineRouterOverrideRules)}
        defaultModel={nineRouterDefaultModel}
        onDefaultModelChange={handleChange(setNineRouterDefaultModel)}
        modelIds={modelIds}
        title="9Router Model Override"
        description="Override models on requests forwarded to 9router. Independent from Global Model Enforcement above."
      />

      <SystemKeysSection />

      <SystemKeyLiveStatus />

      {updateConfig.isSuccess && !dirty && (
        <div className="glass-panel-elevated rounded-3xl p-4 text-center text-sm text-emerald-700 bg-emerald-50/50">
          Configuration saved and applied successfully. Backend cache has been refreshed.
        </div>
      )}
      {updateConfig.isError && (
        <div className="glass-panel-elevated rounded-3xl p-4 text-center text-sm text-error bg-error/5">
          {(updateConfig.error as any)?.response?.data?.detail || "Failed to save configuration"}
        </div>
      )}
    </div>
  );
}
