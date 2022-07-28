import data_functions as dat
import itertools
import math
from metric_functions import *
import multiprocessing as mp
import numpy as np
import os, os.path
import pandas as pd
import pyDOE2 as pyd
import scipy.optimize as sco
import setup_analysis as sa
import time
import ri_water_model as rm

##  build futures
def build_futures(
    df_climate_deltas_annual: pd.DataFrame,
    df_model_data: pd.DataFrame
):
    """
        Build LHS table and all futures.

        - df_climate_deltas_annual: data frame of climate deltas
        - df_model_data: baseline trajectories to modify
    """
    # build lhs samples
    dict_f0_vals, dict_ranges = get_lhs_ranges_and_base_values(df_climate_deltas_annual)
    df_lhs = dat.generate_lhs_samples(sa.n_lhs, dict_ranges, dict_f0_vals, sa.field_key_future)

    # get climate deltas
    dict_climate_factor_delta_field_map = {"flow_m3s": "flow_m3s", "precipitation_mm": "precipitation_mm"}
    df_climate_deltas_by_future = dat.get_climate_factor_deltas(
        df_model_data,
        df_lhs,
        dict_climate_factor_delta_field_map,
        sa.range_delta_base, 
        sa.range_delta_fut, 
        max(sa.model_historical_years),
        field_future_id = sa.field_key_future
    )

    # apply other deltas
    t0 = max(df_model_data[df_model_data[sa.field_time_year] == min(sa.model_projection_years) - 1][sa.field_time_time_period])
    t1 = max(df_model_data[sa.field_time_time_period])
    df_other_deltas_by_future = dat.get_linear_delta_trajectories_by_future(
        df_model_data,
        df_lhs[[x for x in df_lhs.columns if x not in dict_climate_factor_delta_field_map.keys()]],
        t0,
        t1,
        field_future_id = sa.field_key_future
    )
    # merge back in some data
    df_other_deltas_by_future = pd.merge(
        df_other_deltas_by_future, 
        df_model_data[[sa.field_time_time_period, sa.field_time_year, sa.field_time_month]]
    )

    # build final futures table
    df_futures = pd.merge(df_climate_deltas_by_future, df_other_deltas_by_future)
    fields_ind = [sa.field_key_future, sa.field_time_time_period, sa.field_time_year, sa.field_time_month]
    fields_dat = sorted([x for x in df_futures.columns if x not in fields_ind])
    df_futures = df_futures[fields_ind + fields_dat]

    return df_futures, df_lhs



##  build the primary key attribute table
def build_primary_attribute(
    df_fut: pd.DataFrame,
    df_strat: pd.DataFrame,
    field_key_future: str = sa.field_key_future,
    field_key_primary: str = sa.field_key_primary,
    field_key_strategy: str = sa.field_key_strategy
) -> pd.DataFrame:
    
    # create a primary key
    fields_index = [field_key_strategy, field_key_future]
    field_primary_key = field_key_primary
    df_attribute_primary = pd.DataFrame(
        list(itertools.product(
            list(df_strat[field_key_strategy]), 
            list(df_fut[field_key_future])
        )), 
        columns = fields_index
    )
    df_attribute_primary[field_key_primary] = range(len(df_attribute_primary))
    df_attribute_primary = df_attribute_primary[[field_key_primary] + fields_index]
    
    return df_attribute_primary

    
##  get the lhs ranges and base values used to build futures
def get_lhs_ranges_and_base_values(
    df_climate_deltas_annual: pd.DataFrame
) -> tuple:
    """
        Get the lhs ranges and base values used to build futures
        
        - df_climate_deltas_annual: data frame with climate deltas used to set ranges for sampling around climate variables
    """
    #
    #  NOTE: this function would be modified to read these data from a table. For now, we specify the dictionary here
    #
    
    # setup ranges for lhs
    dict_ranges = {
        "flow_m3s": [
            0.95*min(df_climate_deltas_annual["delta_q_2055_annual"]), 
            1.05*max(df_climate_deltas_annual["delta_q_2055_annual"])
        ],
        "precipitation_mm": [
            0.95*min(df_climate_deltas_annual["delta_p_2055_annual"]), 
            1.05*max(df_climate_deltas_annual["delta_p_2055_annual"])
        ],
        "population": [0.8, 1.3],
        "demand_municipal_m3p": [0.8, 1.2],
        "demand_agricultural_m3km2": [0.9, 1.1],
        "area_ag_km2": [0.8, 1.5]
    }
    # set future 0 values - different because we'll apply deltas differently
    dict_f0_vals = {
        "flow_m3s": 0,
        "precipitation_mm": 0,
        "population": 1,
        "demand_municipal_m3p": 1,
        "demand_agricultural_m3km2": 1,
        "area_ag_km2": 1
    }
    
    return dict_f0_vals, dict_ranges



##  use to collect and clean results after running in parallel
def get_metrics_from_node_return(result):
    
    id_primary, df_ret = result
    
    df_ret[sa.field_key_primary] = id_primary
    
    df_metric_1 = get_mean_reservoir(
        df_ret, 
        sa.field_key_primary, 
        sa.field_time_year, 
        10
    )
    
    df_metric_2 = get_mean_groundwater(
        df_ret, 
        sa.field_key_primary, 
        sa.field_time_year, 
        10
    )
    
    df_year_unacceptable, df_metric_3 = get_unacceptable_unmet_demand(
        df_ret,
        field_key_primary = sa.field_key_primary,
        field_measure = "u_2_proportion",
        field_metric_exceed = "exceed_threshes",
        field_metric_prop = "proportion_unacceptable_unmet_demand",
        field_month = sa.field_time_month,
        field_year = sa.field_time_year,
    )
    
    df_metrics_summary = pd.merge(df_metric_1, df_metric_2)
    df_metrics_summary = pd.merge(df_metrics_summary, df_metric_3)

    return df_metrics_summary



## get the output dataframe of metrics of interest
def get_metric_df_out(
    vec_df_out_ri: list,
    fp_template_csv_out: str = None
):
    """
        collect metrics from parallelized return list and form output dataframe
        - vec_df_out_ri: list of raw outputs from pool.async()
    """
    vec_df_metrics = []

    for i in range(len(vec_df_out_ri)):

        # write out the results for the primary id
        if fp_template_csv_out is not None:
            fp_out = fp_template_csv_out%(vec_df_out_ri[i][0])
            vec_df_out_ri[i][1].to_csv(fp_out, index = None, encoding = "UTF-8")

        df_cur = get_metrics_from_node_return(vec_df_out_ri[i])

        if len(vec_df_metrics) == 0:
            vec_df_metrics = [df_cur for x in vec_df_out_ri]
        else:
            vec_df_metrics[i] = df_cur[vec_df_metrics[0].columns]
    vec_df_metrics = pd.concat(vec_df_metrics, axis = 0).reset_index(drop = True)
    
    return vec_df_metrics



## function here
def get_model_data_from_primary_key(
    id_primary: int,
    df_attribute_primary: pd.DataFrame,
    df_futures: pd.DataFrame,
    df_strategies: pd.DataFrame,
    field_primary_key: str = "primary_id",
    field_future: str = "future_id", 
    field_strategy: str = "strategy_id"
):
    row_scenario = df_attribute_primary[df_attribute_primary[field_primary_key] == id_primary]
    # get ids
    id_future = int(row_scenario[field_future])
    id_primary = int(row_scenario[field_primary_key])
    id_strategy = int(row_scenario[field_strategy])

    # get input data
    df_future = df_futures[df_futures[field_future] == id_future].copy()
    df_future.drop([field_future], axis = 1, inplace = True)
    df_strategy = df_strategies[df_strategies[field_strategy] == id_strategy].copy()
    df_strategy.drop([field_strategy], axis = 1, inplace = True)
    df_input_data = pd.merge(df_future, df_strategy)
    
    return df_input_data



## load all input data from CSVs
def load_data(
    fp_climate_deltas: str = sa.fp_csv_climate_deltas_annual,
    fp_model_data: str = sa.fp_csv_baseline_trajectory_model_input_data,
    fp_stratey_inputs: str = sa.fp_xlsx_strategy_inputs,
    field_key_strategy: str = sa.field_key_strategy
):
    
    """
        Load input data for the ri water model
    """
    # load baseline model data
    try:
        df_model_data = pd.read_csv(fp_model_data)
    except:
        raise ValueError(f"Error: model data input file {fp_model_data} not found.")
    
    # load climate deltas
    try:
        df_climate_deltas_annual = pd.read_csv(fp_climate_deltas)
    except:
        raise ValueError(f"Error: annual climate delta input file {df_climate_deltas_annual} not found.")
    
    # load strategies
    try:
        df_attr_strategy, df_strategies = dat.get_strategy_table(
            fp_stratey_inputs, 
            field_strategy_id = field_key_strategy
        )
    except:
        raise ValueError(f"Error in get_strategy_table: check the file at path {fp_stratey_inputs}.")
    
    return df_attr_strategy, df_climate_deltas_annual, df_model_data, df_strategies



##  write a dictionary of dataframes to csvs
def write_output_csvs(
    dir_output: str,
    dict_write: dict = {},
    makedirs: bool = True
):
    """
        Use a dictionary to map a file name to a dataframe out
        
        - dir_output: output directory for files
        - dict_write: dictionary of form {fn_out: df_out, ...}
        - makedirs: make the directory dir_output if it does not exist
    """
    
    if not os.path.exists(dir_output):
        if makedirs:
            os.makedirs(dir_output, exist_ok = True)
        else:
            raise ValueError(f"Error in write_output_csvs: output directory {dir_output} not found. Set makedirs = True to make the directory.")
    
    for fn in dict_write.keys():
        fp_out = os.path.join(dir_output, fn)
        dict_write[fn].to_csv(fp_out, index = None, encoding = "UTF-8")
    
    return True
    
    
    
    
#########################
#    main() FUNCTION    #
#########################

def main():
    
    # read in input data
    df_attr_strategy, df_climate_deltas_annual, df_model_data, df_strategies = load_data()
    
    # sample LHS and build futures 
    df_futures, df_lhs = build_futures(df_climate_deltas_annual, df_model_data)
    
    # built the attribute table and get primary ids to run
    df_attribute_primary = build_primary_attribute(df_lhs, df_attr_strategy)
    all_primaries = list(df_attribute_primary[
        df_attribute_primary[sa.field_key_strategy].isin(sa.strats_run)
    ][sa.field_key_primary])
    
    # start the MP pool for asynchronous parallelization
    t0_par_async = time.time()
    print("Starting pool.async()...")
    
    pool = mp.Pool()
    
    # initialize callback function
    def get_result(result):
        global vec_df_out_ri
        vec_df_out_ri.append(result)

    # loop over primary ids
    for id_primary in all_primaries:
       
        # get data
        df_input_data = get_model_data_from_primary_key(
            id_primary,
            df_attribute_primary,
            df_futures,
            df_strategies,
            sa.field_key_primary,
            sa.field_key_future, 
            sa.field_key_strategy
        )

        # apply to pool
        pool.apply_async(
            rm.ri_water_resources_model,
            args = (
                df_input_data,
                rm.md_dict_initial_states, 
                rm.md_dict_parameters, 
                rm.md_dict_default_levers,
                id_primary
            ),
            callback = get_result
        )
        
    pool.close()
    pool.join()
    # notify some info
    t1_par_async = time.time()
    t_delta = np.round(t1_par_async - t0_par_async, 2)
    print(f"Pool.async() done in {t_delta} seconds.\n")
    # collect output
    df_return = get_metric_df_out(vec_df_out_ri)
    
    # export data
    dir_return = os.path.join(sa.dir_out, sa.analysis_name)
    dict_out = {
        os.path.join(dir_return, sa.fn_csv_attribute_future_id): df_lhs,
        os.path.join(dir_return, sa.fn_csv_attribute_strategy_id): df_attr_strategy,
        os.path.join(dir_return, sa.fn_csv_strategies): df_strategies,
        os.path.join(dir_return, sa.fn_csv_attribute_primary_id): df_attribute_primary,
        os.path.join(dir_return, sa.fn_csv_futures): df_futures,
        os.path.join(dir_return, sa.fn_csv_metrics): df_return
    }
    
    write_output_csvs(dir_return, dict_out)
    
    print("Done.")


    

# run
if __name__ == "__main__":
    
    # initialize callback vector in global context (outside of main())
    vec_df_out_ri = []
    
    # call main
    main()



