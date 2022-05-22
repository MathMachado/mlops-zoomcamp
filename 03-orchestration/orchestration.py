import pandas as pd
import pickle

from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LinearRegression, Lasso, Ridge
from sklearn.metrics import mean_squared_error

import mlflow

import xgboost as xgb

from hyperopt import fmin, tpe, hp, STATUS_OK, Trials
from hyperopt.pyll import scope

from prefect import flow, task

@task
def read_dataframe(filename):
    df = pd.read_parquet(filename)

    df.lpep_dropoff_datetime = pd.to_datetime(df.lpep_dropoff_datetime)
    df.lpep_pickup_datetime = pd.to_datetime(df.lpep_pickup_datetime)

    df['duration'] = df.lpep_dropoff_datetime - df.lpep_pickup_datetime
    df.duration = df.duration.apply(lambda td: td.total_seconds() / 60)

    df = df[(df.duration >= 1) & (df.duration <= 60)]

    categorical = ['PULocationID', 'DOLocationID']
    df[categorical] = df[categorical].astype(str)

    return df

@task
def add_features(df_train, df_val):
    df_train['PU_DO'] = df_train['PULocationID'] + '_' + df_train['DOLocationID']
    df_val['PU_DO'] = df_val['PULocationID'] + '_' + df_val['DOLocationID']

    categorical = ['PU_DO'] #'PULocationID', 'DOLocationID']
    numerical = ['trip_distance']

    dv = DictVectorizer()

    train_dicts = df_train[categorical + numerical].to_dict(orient='records')
    X_train = dv.fit_transform(train_dicts)

    val_dicts = df_val[categorical + numerical].to_dict(orient='records')
    X_val = dv.transform(val_dicts)


    target = 'duration'
    y_train = df_train[target].values
    y_val = df_val[target].values
    return X_train, X_val, y_train, y_val, dv

# def create_lr(X_train, X_val, y_train, y_val, dv):
#     lr = LinearRegression()
#     lr.fit(X_train, y_train)

#     y_pred = lr.predict(X_val)

#     mean_squared_error(y_val, y_pred, squared=False)

#     with open('models/lin_reg.bin', 'wb') as f_out:
#         pickle.dump((dv, lr), f_out)

# def create_lasso(X_train, X_val, y_train, y_val):

#     with mlflow.start_run():

#         mlflow.set_tag("developer", "cristian")

#         mlflow.log_param("train-data-path", "./data/green_tripdata_2021-01.parquet")
#         mlflow.log_param("valid-data-path", "./data/green_tripdata_2021-02.parquet")

#         alpha = 0.1
#         mlflow.log_param("alpha", alpha)
#         lr = Lasso(alpha)
#         lr.fit(X_train, y_train)

#         y_pred = lr.predict(X_val)
#         rmse = mean_squared_error(y_val, y_pred, squared=False)
#         mlflow.log_metric("rmse", rmse)

#         mlflow.log_artifact(local_path="models/lin_reg.bin", artifact_path="models_pickle")

@task
def train_model_search(train, valid, y_val):
    def _objective(params):
        with mlflow.start_run():
            mlflow.set_tag("model", "xgboost")
            mlflow.log_params(params)
            booster = xgb.train(
                params=params,
                dtrain=train,
                num_boost_round=1000,
                evals=[(valid, 'validation')],
                early_stopping_rounds=50
            )
            y_pred = booster.predict(valid)
            rmse = mean_squared_error(y_val, y_pred, squared=False)
            mlflow.log_metric("rmse", rmse)

        return {'loss': rmse, 'status': STATUS_OK}

    search_space = {
        'max_depth': scope.int(hp.quniform('max_depth', 4, 100, 1)),
        'learning_rate': hp.loguniform('learning_rate', -3, 0),
        'reg_alpha': hp.loguniform('reg_alpha', -5, -1),
        'reg_lambda': hp.loguniform('reg_lambda', -6, -1),
        'min_child_weight': hp.loguniform('min_child_weight', -1, 3),
        'objective': 'reg:linear',
        'seed': 42
    }

    best_result = fmin(
        fn=_objective,
        space=search_space,
        algo=tpe.suggest,
        max_evals=1,
        trials=Trials()
    )
    return best_result

@task
def train_best_model(X_train, X_val, y_train, y_val, dv):
    with mlflow.start_run():
        
        train = xgb.DMatrix(X_train, label=y_train)
        valid = xgb.DMatrix(X_val, label=y_val)

        best_params = {
            'learning_rate': 0.09585355369315604,
            'max_depth': 30,
            'min_child_weight': 1.060597050922164,
            'objective': 'reg:linear',
            'reg_alpha': 0.018060244040060163,
            'reg_lambda': 0.011658731377413597,
            'seed': 42
        }

        mlflow.log_params(best_params)

        booster = xgb.train(
            params=best_params,
            dtrain=train,
            num_boost_round=1000,
            evals=[(valid, 'validation')],
            early_stopping_rounds=50
        )

        y_pred = booster.predict(valid)
        rmse = mean_squared_error(y_val, y_pred, squared=False)
        mlflow.log_metric("rmse", rmse)

        with open("models/preprocessor.b", "wb") as f_out:
            pickle.dump(dv, f_out)
        mlflow.log_artifact("models/preprocessor.b", artifact_path="preprocessor")

        mlflow.xgboost.log_model(booster, artifact_path="models_mlflow")

@flow
def main_flow(train_path: str = './data/green_tripdata_2021-01.parquet', 
                val_path: str = './data/green_tripdata_2021-02.parquet'):
    mlflow.set_tracking_uri("sqlite:///mlflow.db")
    mlflow.set_experiment("nyc-taxi-experiment")
    # Load
    df_train = read_dataframe(train_path)
    df_val = read_dataframe(val_path)

    # Transform
    X_train, X_val, y_train, y_val, dv = add_features(df_train, df_val).result()

    # Training
    train = xgb.DMatrix(X_train, label=y_train)
    valid = xgb.DMatrix(X_val, label=y_val)
    best = train_model_search(train, valid, y_val)
    train_best_model(X_train, X_val, y_train, y_val, dv, wait_for=best)

# main_flow()

from prefect.deployments import DeploymentSpec
from prefect.orion.schemas.schedules import IntervalSchedule
from prefect.flow_runners import SubprocessFlowRunner
from datetime import timedelta

DeploymentSpec(
    flow=main_flow,
    name="model_training",
    # schedule=IntervalSchedule(interval=timedelta(weeks=1)),
    flow_runner=SubprocessFlowRunner(),
    tags=["ml"],
)