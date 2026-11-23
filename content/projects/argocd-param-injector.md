---
title: GitOps인데 Git에 아무것도 없다?
summary: Argo CD Config Management Plugin을 직접 개발해 Secret과 이미지 태그를 Parameter Store에서 렌더링 시점에 주입, GitOps 저장소에 민감 정보와 태그 커밋을 남기지 않도록 전환
field: 자동화
competency: k8s
stack: Argo CD, Parameter Store, EKS
order: 1
---
### 우리 팀이 GitOps에서 이미지 태그와 Secret을 관리하는 방법

쿠버네티스에서는 민감한 정보를 secret이라는 리소스에 저장한다. 하지만 GitOps를 운영하다 보면 Repository에 secret 리소스가 정의되고 base64로 인코딩한다고 해도 <b>암호화(encryption)가 아니라 인코딩</b>이므로 위험할 수 있다.

민감 정보를 Repository 외에서 별도로 관리할 방법이 없는지 검토하던 중 <b>Argo CD의 Config Management Plugins(CMP)</b> 기능을 확인하고 적용했다.

### 고려 사항

ecs → eks 마이그레이션 <b>과정</b>에서 기존에 사용하던 parameter store를 활용할 수 있어야 한다.

- fargate에서도 자유롭게 사용할 수 있어야 한다.
- 복잡하지 않고 최대한 편리해야 한다.
- 이 외에도 고려할 사항이 많았다.

### 검토했던 방법들

- <b>ASCP</b><br />AWS EKS 공식 Docs에서 제시하던 방법이지만 Fargate에서 사용할 수 없어 제외
- <b>external-secrets(External Secrets 계열)</b><br /> 다양한 secret 저장소(vault, parameter store 등)을 연동하는 오픈소스이지만 argocd에서 manifest 렌더링 과정에서 불러오는 방식이 아니라 별도의 동기화 주기가 있다.
- <b>plugin에 sops 적용</b><br /> 이것은 repository에서 민감 정보를 관리하기 위해 연구했던 방법이지만, 기존 방식과 차이가 커 보류했다.

### 참고

<b>1. Config Management Plugins란?</b>

Argo CD가 기본으로 제공하는 Helm, Kustomize, Ksonnet, Plain-YAML 외에, 사용자 정의 로직으로 매니페스트를 생성할 수 있게 하는 기능이다.

빌드 시 특정 스크립트를 실행하여 YAML을 동적으로 수정하거나, 사내에서 개발한 툴을 통해 매니페스트를 생성하려고 할 때 활용할 수 있다.

### Plugin 구성 방식

plugin은 argocd-repo-server Pod에 sidecar 컨테이너로 실행되어 동작한다.

ConfigManagementPlugin 구현방법은 2가지가 있다.

- plugin.yaml을 argocd-cm에서 configmap으로 구현하고 extracontainer에 volumemount로 삽입한다.
- extracontainer로 실행할 이미지를 직접 구현하여 컨테이너 내부에 plugin.yaml을 직접 작성한다.

python 스크립트를 실행하기 위해 후자의 방식을 선택해 구성했다.

## 실제 Plugin 구성

plugin은 아래와 같이 3단계로 구성된다.

<b>(옵션) discover 단계</b>

Git 저장소를 스캔해, “어느 저장소가 이 플러그인을 사용할지”를 자동으로 판별하는 설정이다.<br />discover를 정의하지 않으면 적용할 argocd Application에 <code>spec.source.plugin.name</code>에 명시해야 한다.<br />command가 실행된 후 파일명이 출력되어야 한다.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ConfigManagementPlugin
metadata:
  # The name of the plugin must be unique within a given Argo CD instance.
  name: param-injector
spec:
  discover:
    find:
      # *.yaml 파일 중 param-injector.enabled 가 true 인 파일을 찾는다.
      command: [ "sh", "-c", "find . -name '*.yaml' -exec sh -c '...생략...' \; -print" ]
  init:
    # python 스크립트를 통해 yaml파일을 수정한다.
    command: ["sh", "-c", "python3 /home/argocd/init.py"]
  generate:
    # 기존 방식 그대로 kustomize를 통해 manifest를 렌더링 한다.
    command: ["sh", "-c", "kustomize build . --enable-helm --load-restrictor LoadRestrictionsNone"]
```

application 폴더에 존재하는 yaml파일에 param-injector.enabled 값이 true이면 plugin을 적용한다.

### init 단계

Argo CD가 플러그인을 통해 매니페스트를 생성하기 직전에, 한 번만 실행되는 스크립트 단계이다.<br />python 스크립트가 yaml파일을 스캔하면서 \<path:/...> 형식의 값이 정의되어 있으면 해당 값을 parameter store 값에서 조회해 치환한다.

### generate 단계

최종적으로 쿠버네티스 매니페스트를 생성하는 로직을 담는다. <br />generate 단계에서는 반드시 쿠버네티스 리소스가 정의된 yaml파일이 생성되어야 한다.

![](https://cdn-images-1.medium.com/max/1024/1*k4bYHOR0v6riacV1e6_gFw.png)

### Plugin 컨테이너 생성

```docker
# Dockerfile
FROM alpine:latest
# apk 패키지 목록 갱신 후, 필요한 도구 설치
RUN apk update && apk add --no-cache \
    bash \
    curl \
    unzip \
    helm \
    yq \
    kustomize \
    python3 \
    py3-pip \
    py3-yaml \
    py3-boto3
# /usr/bin 에 설치된 실행 파일을 /usr/local/bin 으로 심볼릭 링크
RUN ln -s /usr/bin/curl       /usr/local/bin/curl       \
 && ln -s /usr/bin/unzip      /usr/local/bin/unzip      \
 && ln -s /usr/bin/helm       /usr/local/bin/helm       \
 && ln -s /usr/bin/kustomize  /usr/local/bin/kustomize  \
 && ln -s /usr/bin/yq         /usr/local/bin/yq         \
 && ln -s /usr/bin/python3    /usr/local/bin/python3
# 누구나 실행 가능하도록 권한 설정
RUN chmod 755 /usr/local/bin/curl       \
              /usr/local/bin/unzip      \
              /usr/local/bin/helm       \
              /usr/local/bin/kustomize  \
              /usr/local/bin/yq         \
              /usr/local/bin/python3
# argocd 사용자 생성
RUN adduser -D -u 999 -G root argocd
# tag_updater.sh 파일을 복사하고 실행 권한 설정
WORKDIR /home/argocd
COPY init.py ./
# plugin.yaml 파일을 복사
WORKDIR /home/argocd/cmp-server/config/
COPY plugin.yaml ./
# 원하는 경우, 실행 진입점을 Bash로 설정
# (필요 없다면 주석처리 가능)
```

필요한 명령어 및 라이브러리들을 설치한다.

argocd에서 plugin컨테이너는 <b>특정 non-root 사용자(예: 999)</b> 로 동작할 수 있어 해당 사용자로 실행되도록 구성했다.

<code>init.py</code>를 특정 경로에 저장한다. 경로는 임의로 지정할 수 있다.

/home/argocd/cmp-server/config/ 경로에 위에서 작성한 plugin.yaml을 배치한다. 경로는 고정이다.

## ArgoCD Configmap 수정

ArgoCD Helm Chart를 기준으로 한다.

### Assume Role 생성

parameter store가 저장된 계정의 콘솔에 argocd가 assume 할 role 생성

parameter decrypt 하기위한 kms 권한과 getparameter 권한을 부여한다.

```yaml
repoServer:
  # 각 cluster 마다 정의
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: "<ROLE_ARN>"
```

```yaml
# helm chart 공통 value에 정의
extraContainers:
- name: param-injector
  command:
  - "/var/run/argocd/argocd-cmp-server"
  image: "<ECR_IMAGE_URI>:<TAG>"
  securityContext:
    runAsUser: 999
  volumeMounts:
  - mountPath: /var/run/argocd
    name: var-files
  - mountPath: /home/argocd/cmp-server/plugins
    name: plugins
  - mountPath: /tmp
    name: cmp-tmp
```

```yaml
volumes:
- name: cmp-tmp
  emptyDir: {}
```

## 사용법

![](https://cdn-images-1.medium.com/max/690/1*A2kCKWmZGCE-GmK30Xn2tg.png)

yaml파일에서 parameter store를 적용할 부분을<br /> \<path:/image\_tag/sample-app/development> 같은 형식으로 입력한다.

```yaml
param-injector:
  enabled: true
  accoundId: 123456789123
```

param-injector 플러그인 적용을 위해 <b>accountId</b> 값도 설정한다.

## 배포 Pipeline 적용

param-injector가 secret 변수 관리를 위한 것도 있지만, GitOps 배포 파이프라인에서도 유용하게 활용됐다.

우리 회사는 여러 이유로 인해 ArgoCD의 Image Updater를 사용할 수 없어 배포할 때마다 Git에 컨테이너 이미지 Tag 값을 수정하고 Push 해야 했다.

그렇게 할 경우 커밋 내역이 과도하게 누적되고 Application 수가 많아질수록 argocd repo-server의 부하도 점점 증가할 것이다.

하지만, param-injector를 이용하여 위와 같은 문제 점을 해결할 수 있었다.

### 동작 흐름

parameter store 경로<br /> /image\_tag/\{service\}/\{environment\}

```bash
# put image tag to parameter store
aws ssm put-parameter --name "/image_tag/$IMAGE_NAME/$VAR_ENVIRONMENT" --value "$IMAGE_TAG" --type String --overwrite
```

파이프라인에서 Parameter store에 image tag 값을 cli로 업데이트한다.

```bash
# Deploy the application using ArgoCD (공개용: 토큰 기반)
argocd login "<ARGOCD_DOMAIN>" --auth-token "$ARGOCD_AUTH_TOKEN" --grpc-web
argocd app get --hard-refresh "$ARGOCD_APP_NAME" $ARGOCD_OPTS
argocd app sync "$ARGOCD_APP_NAME" $ARGOCD_OPTS
argocd app wait "$ARGOCD_APP_NAME" --timeout 900 $ARGOCD_OPTS
```

argocd application을 hard-refresh 한다. eks repo의 변경사항이 없기때문에 일반 refresh로는 반영되지 않는다.

## 정리하며

EKS를 도입하면서 Secret을 어떤 방식으로 관리할지 검토하는 과정에서 이 플러그인을 적용했다. 결과적으로 Secret을 리포지토리에 직접 저장하지 않으면서도, ArgoCD 렌더링 흐름 안에서 값을 주입할 수 있어 목적에 부합했고, 배포 파이프라인에서도 이미지 태그 등 변경 포인트를 유연하게 처리하는 데 도움이 됐다.

다만 적용 범위에는 한계가 있다. 현재 구조에서는 Application이 직접 참조하는 폴더 내 YAML에만 플러그인이 적용되기 때문에, kustomize로 외부 경로에서 가져오는 YAML에는 동일한 방식으로 주입이 이뤄지지 않는다. 이 부분은 운영하면서 체감되는 제약이므로, 적용 범위를 확장할 수 있는 방향으로 개선이 필요하다.
