{{/* Nom des objets : celui de la release, sauf si elle contient déjà « fabrique ». */}}
{{- define "fabrique.fullname" -}}
{{- if contains "fabrique" .Release.Name -}}{{ .Release.Name | trunc 50 | trimSuffix "-" }}{{- else -}}{{ printf "%s-fabrique" .Release.Name | trunc 50 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "fabrique.labels" -}}
app.kubernetes.io/name: fabrique
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "fabrique.selector" -}}
app.kubernetes.io/name: fabrique
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "fabrique.secretName" -}}
{{- .Values.secrets.existingSecret | default (include "fabrique.fullname" .) -}}
{{- end -}}

{{- define "fabrique.image" -}}
{{- printf "%s/%s:%s" .root.Values.image.registry .name (.root.Values.image.tag | toString) -}}
{{- end -}}

{{/* Variables de proxy du dashboard et du runner (proxy d'entreprise). Les services internes sont toujours joints en direct. */}}
{{- define "fabrique.proxyEnv" -}}
{{- if .Values.outboundProxy.url }}
{{- $n := include "fabrique.fullname" . }}
{{- $no := printf "localhost,127.0.0.1,%s,%s-runner,%s-launcher,%s-proxy,.svc,.cluster.local" $n $n $n $n }}
{{- if .Values.outboundProxy.noProxy }}{{ $no = printf "%s,%s" $no .Values.outboundProxy.noProxy }}{{ end }}
- {name: HTTPS_PROXY, value: {{ .Values.outboundProxy.url | quote }}}
- {name: HTTP_PROXY, value: {{ .Values.outboundProxy.url | quote }}}
- {name: NO_PROXY, value: {{ $no | quote }}}
- {name: https_proxy, value: {{ .Values.outboundProxy.url | quote }}}
- {name: http_proxy, value: {{ .Values.outboundProxy.url | quote }}}
- {name: no_proxy, value: {{ $no | quote }}}
{{- end }}
{{- end -}}

{{/* Adresse publique : la valeur fournie, sinon celle de l'Ingress, sinon l'adresse du Service dans le cluster. */}}
{{- define "fabrique.publicUrl" -}}
{{- if .Values.publicUrl -}}{{ ternary .Values.publicUrl (printf "https://%s" .Values.publicUrl) (contains "://" .Values.publicUrl) }}
{{- else if .Values.ingress.enabled -}}{{ ternary "https" "http" .Values.ingress.tls.enabled }}://{{ .Values.ingress.host }}
{{- else -}}http://{{ include "fabrique.fullname" . }}.{{ .Release.Namespace }}.svc:{{ .Values.service.port }}
{{- end -}}
{{- end -}}
