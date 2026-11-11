# Optional source-integration fixtures

Set `FINIX_TEST_SDK_SOURCE` to your own checked-out Hermes SDK source directory.
The integration fixtures read the real SDK producer source without changing it.
Use the exact reviewed SDK checkout for reproducible results; native integration
requires Hermes plugin SDK 0.23.0. This repository does not vendor the SDK.
The Node route-source fixture requires this variable; the Python pure-admission
suite explicitly skips only its SDK-producer test when it is unset. All pure
admission assertions still execute. Missing SDK tests are not a full pass.
